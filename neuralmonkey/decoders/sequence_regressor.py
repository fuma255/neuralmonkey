from typing import cast, Any, Callable, Iterable, Optional, List

import tensorflow as tf

from neuralmonkey.dataset import Dataset
from neuralmonkey.nn.projection import multilayer_projection, linear
from neuralmonkey.model.model_part import ModelPart, FeedDict
from neuralmonkey.nn.mlp import MultilayerPerceptron
from neuralmonkey.checking import assert_shape

# tests: lint, mypy

# pylint: disable=too-many-instance-attributes


class SequenceRegressor(ModelPart):
    """A simple MLP regression over encoders.

    The API pretends it is an RNN decoder which always generates a sequence of
    length exactly one.
    """
    # pylint: disable=too-many-arguments
    def __init__(self,
                 name: str,
                 encoders: List[Any],
                 data_id: str,
                 layers: Optional[List[int]]=None,
                 activation_fn: Callable[[tf.Tensor], tf.Tensor]=tf.tanh,
                 dropout_keep_prob: float=0.5,
                 save_checkpoint: Optional[str]=None,
                 load_checkpoint: Optional[str]=None) -> None:
        ModelPart.__init__(self, name, save_checkpoint, load_checkpoint)

        self.encoders = encoders
        self.data_id = data_id
        self.layers = layers
        self.activation_fn = activation_fn
        self.dropout_keep_prob = dropout_keep_prob
        self.max_output_len = 1

        with tf.variable_scope(name):
            self.learning_step = tf.get_variable(
                "learning_step", [], trainable=False,
                initializer=tf.constant_initializer(0))

            self.gt_inputs = tf.placeholder(tf.float32, shape=[None],
                                            name="targets")
            self.train_mode = tf.placeholder(tf.bool, shape=[],
                                         name="mode_placeholder")

            mlp_input = tf.concat([enc.encoded for enc in encoders], 1)
            mlp_input = tf.subtract(encoders[0].encoded, encoders[1].encoded)

            mlp = multilayer_projection(mlp_input, layers,
                                        activation=self.activation_fn,
                                        train_mode=self.train_mode,
                                        dropout_keep_prob=self.dropout_keep_prob)
            # TODO extend it to output into multidimensional space
            mlp_logits = linear(mlp, 1)

            # mlp_logits = tf.contrib.layers.fully_connected(mlp_input, 1, biases_initializer=tf.zeros_initializer(), activation_fn=None)

            assert_shape(mlp_logits, [-1, 1])

            self.predicted = mlp_logits

            self.cost = tf.reduce_sum(tf.square(mlp_logits - tf.expand_dims(self.gt_inputs, 1)))
            # self.cost = tf.Print(tf.reduce_sum(tf.square(mlp_logits - tf.expand_dims(self.gt_inputs, 1))),
            #                      [(tf.reduce_mean(encoders[0].encoded, axis=1)-tf.reduce_mean(encoders[1].encoded, axis=1))*(1-self.gt_inputs),self.gt_inputs], #tf.reduce_mean(mlp_input, axis=1)
                                 # summarize=10000)

            tf.summary.scalar(
                'val_optimization_cost', self.cost,
                collections=["summary_val"])
            tf.summary.scalar(
                'train_optimization_cost',
                self.cost, collections=["summary_train"])
    # pylint: enable=too-many-arguments

    @property
    def train_loss(self):
        return self.cost

    @property
    def runtime_loss(self):
        return self.cost

    @property
    def decoded(self):
        return self.predicted

    def feed_dict(self, dataset: Dataset, train: bool=False) -> FeedDict:
        sentences = cast(Iterable[List[str]],
                         dataset.get_series(self.data_id, allow_none=True))

        sentences_list = list(sentences) if sentences is not None else None

        fd = {}  # type: FeedDict
        if sentences_list is not None:
            fd[self.gt_inputs] = list(zip(*sentences_list))[0]

        fd[self.train_mode] = train

        return fd
