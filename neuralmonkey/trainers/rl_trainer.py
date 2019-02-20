"""Training objectives for reinforcement learning."""

from typing import Callable

import numpy as np
import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.decoders.decoder import Decoder
from neuralmonkey.decorators import tensor
from neuralmonkey.logging import warn
from neuralmonkey.trainers.generic_trainer import Objective
from neuralmonkey.vocabulary import END_TOKEN, PAD_TOKEN


# pylint: disable=invalid-name
RewardFunction = Callable[[np.ndarray, np.ndarray], np.ndarray]
# pylint: enable=invalid-name


# pylint: disable=too-few-public-methods,too-many-locals
class ReinforceObjective(Objective[Decoder]):

    # pylint: disable=too-many-arguments
    def __init__(self,
                 decoder: Decoder,
                 reward_function: RewardFunction,
                 subtract_baseline: bool = False,
                 normalize: bool = False,
                 temperature: float = 1.,
                 ce_smoothing: float = 0.,
                 alpha: float = 1.,
                 sample_size: int = 1) -> None:
        """Construct RL objective for training with sentence-level feedback.

        Depending on the options the objective corresponds to:
        1) sample_size = 1, normalize = False, ce_smoothing = 0.0
        Bandit objective (Eq. 2) described in 'Bandit Structured Prediction for
        Neural Sequence-to-Sequence Learning'
        (http://www.aclweb.org/anthology/P17-1138)
        It's recommended to set subtract_baseline = True.
        2) sample_size > 1, normalize = True, ce_smoothing = 0.0
        Minimum Risk Training as described in 'Minimum Risk Training for Neural
        Machine Translation' (http://www.aclweb.org/anthology/P16-1159 Eq. 12).
        3) sample_size > 1, normalize = False, ce_smoothing = 0.0
        The Google 'Reinforce' objective as proposed in 'Google’s NMT System:
        Bridging the Gap between Human and Machine Translation'
        (https://arxiv.org/pdf/1609.08144.pdf) (Eq. 8).
        4) sample_size > 1, normalize = False, ce_smoothing > 0.0
        Google's 'Mixed' objective in the above paper (Eq. 9),
        where ce_smoothing implements alpha.

        Note that 'alpha' controls the sharpness of the normalized distribution
        while 'temperature' controls the sharpness during sampling.

        :param decoder: a recurrent decoder to sample from
        :param reward_function: any evaluator object
        :param subtract_baseline: avg reward is subtracted from obtained reward
        :param normalize: the probabilities of the samples are re-normalized
        :param sample_size: number of samples to obtain feedback for
        :param ce_smoothing: add cross-entropy with this coefficient to loss
        :param alpha: determines the shape of the normalized distribution
        :param temperature: the softmax temperature for sampling
        """
        check_argument_types()
        name = "{}_rl".format(decoder.name)
        Objective[Decoder].__init__(self, name, decoder)

        self.reward_function = reward_function
        self.subtract_baseline = subtract_baseline
        self.normalize = normalize
        self.temperature = temperature
        self.ce_smoothing = ce_smoothing
        self.alpha = alpha
        self.sample_size = sample_size
    # pylint: enable=too-many-arguments

    @tensor
    def loss(self) -> tf.Tensor:

        reference = self.decoder.train_inputs

        def _score_with_reward_function(references: np.array,
                                        hypotheses: np.array) -> np.array:
            """Score (time, batch) arrays with sentence-based reward function.

            Parts of the sentence after generated <pad> or </s> are ignored.
            BPE-postprocessing is also included.

            :param references: indices of references, shape (time, batch)
            :param hypotheses: indices of hypotheses, shape (time, batch)
            :return: an array of batch length with float rewards
            """
            rewards = []
            for refs, hyps in zip(references.transpose(),
                                  hypotheses.transpose()):
                ref_seq = []
                hyp_seq = []
                for r_token in refs:
                    token = self.decoder.vocabulary.index_to_word[r_token]
                    if token in (END_TOKEN, PAD_TOKEN):
                        break
                    ref_seq.append(token)
                for h_token in hyps:
                    token = self.decoder.vocabulary.index_to_word[h_token]
                    if token in (END_TOKEN, PAD_TOKEN):
                        break
                    hyp_seq.append(token)
                # join BPEs, split on " " to prepare list for evaluator
                refs_tokens = " ".join(ref_seq).replace("@@ ", "").split(" ")
                hyps_tokens = " ".join(hyp_seq).replace("@@ ", "").split(" ")
                reward = float(self.reward_function([hyps_tokens],
                                                    [refs_tokens]))
                rewards.append(reward)
            return np.array(rewards, dtype=np.float32)

        samples_rewards = []
        samples_logprobs = []

        for _ in range(self.sample_size):
            # sample from logits
            # decoded, shape (time, batch)
            sample_loop_result = self.decoder.decoding_loop(
                train_mode=False, sample=True, temperature=self.temperature)
            sample_logits = sample_loop_result.histories.logits
            sample_decoded = sample_loop_result.histories.output_symbols

            # rewards, shape (batch)
            # simulate from reference
            sample_reward = tf.py_func(_score_with_reward_function,
                                       [reference, sample_decoded],
                                       tf.float32)

            # pylint: disable=invalid-unary-operand-type
            word_logprobs = -tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=sample_decoded, logits=sample_logits)

            # sum word log prob to sentence log prob
            # no masking here, since otherwise shorter sentences are preferred
            sent_logprobs = tf.reduce_sum(word_logprobs, axis=0)

            samples_rewards.append(sample_reward)   # sample_size x batch
            samples_logprobs.append(sent_logprobs)  # sample_size x batch

        # stack samples, sample_size x batch
        samples_rewards_stacked = tf.stack(samples_rewards)
        samples_logprobs_stacked = tf.stack(samples_logprobs)

        if self.subtract_baseline:
            # if specified, compute the average reward baseline
            reward_counter = tf.Variable(0.0, trainable=False,
                                         name="reward_counter")
            reward_sum = tf.Variable(0.0, trainable=False, name="reward_sum")
            # increment the cumulative reward
            reward_counter = tf.assign_add(
                reward_counter,
                tf.to_float(self.decoder.batch_size * self.sample_size))
            # sum over batch and samples
            reward_sum = tf.assign_add(reward_sum,
                                       tf.reduce_sum(samples_rewards_stacked))
            # compute baseline: avg of previous rewards
            baseline = tf.div(reward_sum,
                              tf.maximum(reward_counter, 1.0))
            samples_rewards_stacked -= baseline

            tf.summary.scalar(
                "train_{}/rl_reward_baseline".format(self.decoder.data_id),
                tf.reduce_mean(baseline), collections=["summary_train"])

        if self.normalize:
            # normalize over sample space
            samples_logprobs_stacked = tf.nn.softmax(
                samples_logprobs_stacked * self.alpha, dim=0)

        scored_probs = tf.stop_gradient(
            tf.negative(samples_rewards_stacked)) * samples_logprobs_stacked

        # sum over samples
        total_loss = tf.reduce_sum(scored_probs, axis=0)

        # average over batch
        batch_loss = tf.reduce_mean(total_loss)

        if self.ce_smoothing > 0.0:
            batch_loss += tf.multiply(self.ce_smoothing, self.decoder.cost)

        tf.summary.scalar(
            "train_{}/self_rl_cost".format(self.decoder.data_id),
            batch_loss,
            collections=["summary_train"])

        return batch_loss


# compatibility function
def rl_objective(*args, **kwargs) -> ReinforceObjective:
    warn("Using deprecated rl_objective function. Use ReinforceObjective class"
         " directly.")
    return ReinforceObjective(*args, **kwargs)
