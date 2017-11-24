from typing import Tuple, List, NamedTuple, Union, Callable, cast, Set

import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.model.stateful import (
    TemporalStatefulWithOutput, TemporalStateful)
from neuralmonkey.model.model_part import ModelPart, FeedDict
from neuralmonkey.nn.ortho_gru_cell import OrthoGRUCell, NematusGRUCell
from neuralmonkey.nn.utils import dropout
from neuralmonkey.vocabulary import Vocabulary
from neuralmonkey.dataset import Dataset
from neuralmonkey.decorators import tensor
from neuralmonkey.model.sequence import (
    EmbeddedSequence, EmbeddedFactorSequence)

RNN_CELL_TYPES = {
    "NematusGRU": NematusGRUCell,
    "GRU": OrthoGRUCell,
    "LSTM": tf.nn.rnn_cell.LSTMCell
}

RNN_DIRECTIONS = ["forward", "backward", "both"]


# pylint: disable=invalid-name
RNNCellTuple = Tuple[tf.nn.rnn_cell.RNNCell, tf.nn.rnn_cell.RNNCell]

RNNSpec = NamedTuple("RNNSpec", [("size", int),
                                 ("direction", str),
                                 ("cell_type", str)])

RNNSpecTuple = Union[Tuple[int], Tuple[int, str], Tuple[int, str, str]]
# pylint: enable=invalid-name


def _make_rnn_spec(size: int,
                   direction: str = "both",
                   cell_type: str = "GRU") -> RNNSpec:
    if size <= 0:
        raise ValueError(
            "RNN size must be a positive integer. {} given.".format(size))

    if direction not in RNN_DIRECTIONS:
        raise ValueError("RNN direction must be one of {}. {} given."
                         .format(str(RNN_DIRECTIONS), direction))

    if cell_type not in RNN_CELL_TYPES:
        raise ValueError("RNN cell type must be one of {}. {} given."
                         .format(str(RNN_CELL_TYPES), cell_type))

    return RNNSpec(size, direction, cell_type)


def _make_rnn_cell(spec: RNNSpec) -> Callable[[], tf.nn.rnn_cell.RNNCell]:
    """Return the graph template for creating RNN cells."""
    return RNN_CELL_TYPES[spec.cell_type](spec.size)


class RecurrentEncoder(ModelPart, TemporalStatefulWithOutput):

    # pylint: disable=too-many-arguments
    def __init__(self,
                 name: str,
                 input_sequence: TemporalStateful,
                 rnn_size: int,
                 rnn_cell: str = "GRU",
                 rnn_direction: str = "both",
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        """Create a new instance of a recurrent encoder."""
        ModelPart.__init__(self, name, save_checkpoint, load_checkpoint)
        TemporalStatefulWithOutput.__init__(self)
        check_argument_types()

        self.input_sequence = input_sequence
        self.dropout_keep_prob = dropout_keep_prob
        self.rnn_spec = _make_rnn_spec(rnn_size, rnn_direction, rnn_cell)

        if self.dropout_keep_prob <= 0.0 or self.dropout_keep_prob > 1.0:
            raise ValueError("Dropout keep prob must be inside (0,1].")

        with self.use_scope():
            self.train_mode = tf.placeholder(tf.bool, [], "train_mode")
    # pylint: enable=too-many-arguments

    @tensor
    def rnn_input(self) -> tf.Tensor:
        return dropout(self.input_sequence.temporal_states,
                       self.dropout_keep_prob, self.train_mode)

    @tensor
    def rnn(self) -> Tuple[tf.Tensor, tf.Tensor]:
        if self.rnn_spec.direction == "both":
            fw_cell = _make_rnn_cell(self.rnn_spec)
            bw_cell = _make_rnn_cell(self.rnn_spec)

            outputs_tup, states_tup = tf.nn.bidirectional_dynamic_rnn(
                fw_cell, bw_cell, self.rnn_input,
                sequence_length=self.input_sequence.lengths,
                dtype=tf.float32)

            outputs = tf.concat(outputs_tup, 2)

            if self.rnn_spec.cell_type == "LSTM":
                states_tup = (state.h for state in states_tup)

            final_state = tf.concat(list(states_tup), 1)
        else:
            rnn_input = self.rnn_input
            if self.rnn_spec.direction == "backward":
                rnn_input = tf.reverse_sequence(
                    self.rnn_input, self.input_sequence.lengths, seq_axis=1)

            cell = _make_rnn_cell(self.rnn_spec)
            outputs, final_state = tf.nn.dynamic_rnn(
                cell, rnn_input, sequence_length=self.input_sequence.lengths,
                dtype=tf.float32)

            if self.rnn_spec.direction == "backward":
                outputs = tf.reverse_sequence(
                    outputs, self.input_sequence.lengths, seq_axis=1)

            if self.rnn_spec.cell_type == "LSTM":
                final_state = final_state.h

        return outputs, final_state

    @tensor
    def temporal_states(self) -> tf.Tensor:
        # pylint: disable=unsubscriptable-object
        return self.rnn[0]
        # pylint: enable=unsubscriptable-object

    @tensor
    def temporal_mask(self) -> tf.Tensor:
        return self.input_sequence.temporal_mask

    @tensor
    def output(self) -> tf.Tensor:
        # pylint: disable=unsubscriptable-object
        return self.rnn[1]
        # pylint: enable=unsubscriptable-object

    def get_dependencies(self) -> Set[ModelPart]:
        """Collect recusively all encoders and decoders."""
        deps = ModelPart.get_dependencies(self)

        # feed only if needed
        if isinstance(self.input_sequence, ModelPart):
            feedable = cast(ModelPart, self.input_sequence)
            deps = deps.union(feedable.get_dependencies())

        return deps

    def feed_dict(self, dataset: Dataset, train: bool = False) -> FeedDict:
        return {self.train_mode: train}


class SentenceEncoder(RecurrentEncoder):
    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(self,
                 name: str,
                 vocabulary: Vocabulary,
                 data_id: str,
                 embedding_size: int,
                 rnn_size: int,
                 rnn_cell: str = "GRU",
                 rnn_direction: str = "both",
                 max_input_len: int = None,
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        """Create a new instance of the sentence encoder."""

        # TODO Think this through.
        s_ckp = "input_{}".format(save_checkpoint) if save_checkpoint else None
        l_ckp = "input_{}".format(load_checkpoint) if load_checkpoint else None

        # TODO! Representation runner needs this. It is not simple to do it in
        # recurrent encoder since there may be more source data series. The
        # best way could be to enter the data_id parameter manually to the
        # representation runner
        self.data_id = data_id

        input_sequence = EmbeddedSequence(
            name="{}_input".format(name),
            vocabulary=vocabulary,
            data_id=data_id,
            embedding_size=embedding_size,
            max_length=max_input_len,
            save_checkpoint=s_ckp,
            load_checkpoint=l_ckp)

        RecurrentEncoder.__init__(
            self,
            name=name,
            input_sequence=input_sequence,
            rnn_size=rnn_size,
            rnn_cell=rnn_cell,
            rnn_direction=rnn_direction,
            dropout_keep_prob=dropout_keep_prob,
            save_checkpoint=save_checkpoint,
            load_checkpoint=load_checkpoint)
    # pylint: enable=too-many-arguments,too-many-locals


class FactoredEncoder(RecurrentEncoder):
    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(self,
                 name: str,
                 vocabularies: List[Vocabulary],
                 data_ids: List[str],
                 embedding_sizes: List[int],
                 rnn_size: int,
                 rnn_cell: str = "GRU",
                 rnn_direction: str = "both",
                 max_input_len: int = None,
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        """Create a new instance of the sentence encoder."""
        s_ckp = "input_{}".format(save_checkpoint) if save_checkpoint else None
        l_ckp = "input_{}".format(load_checkpoint) if load_checkpoint else None

        input_sequence = EmbeddedFactorSequence(
            name="{}_input".format(name),
            vocabularies=vocabularies,
            data_ids=data_ids,
            embedding_sizes=embedding_sizes,
            max_length=max_input_len,
            save_checkpoint=s_ckp,
            load_checkpoint=l_ckp)

        RecurrentEncoder.__init__(
            self,
            name=name,
            input_sequence=input_sequence,
            rnn_size=rnn_size,
            rnn_cell=rnn_cell,
            rnn_direction=rnn_direction,
            dropout_keep_prob=dropout_keep_prob,
            save_checkpoint=save_checkpoint,
            load_checkpoint=load_checkpoint)
    # pylint: enable=too-many-arguments,too-many-locals


class DeepSentenceEncoder(RecurrentEncoder):
    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(self,
                 name: str,
                 vocabulary: Vocabulary,
                 data_id: str,
                 embedding_size: int,
                 rnn_sizes: List[int],
                 rnn_directions: List[str],
                 rnn_cell: str = "GRU",
                 max_input_len: int = None,
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        """Create a new instance of the sentence encoder."""
        check_argument_types()

        if len(rnn_sizes) != len(rnn_directions):
            raise ValueError("Different number of rnn sizes and directions.")

        # TODO Think this through.
        s_ckp = "input_{}".format(save_checkpoint) if save_checkpoint else None
        l_ckp = "input_{}".format(load_checkpoint) if load_checkpoint else None

        # TODO! Representation runner needs this. It is not simple to do it in
        # recurrent encoder since there may be more source data series. The
        # best way could be to enter the data_id parameter manually to the
        # representation runner
        self.data_id = data_id

        prev_layer = EmbeddedSequence(
            name="{}_input".format(name),
            vocabulary=vocabulary,
            data_id=data_id,
            embedding_size=embedding_size,
            max_length=max_input_len,
            save_checkpoint=s_ckp,
            load_checkpoint=l_ckp)

        for level, (rnn_size, rnn_direction) in enumerate(
                zip(rnn_sizes[:-1], rnn_directions)):

            s_ckp = "{}_layer_{}".format(
                save_checkpoint, level) if save_checkpoint else None
            l_ckp = "{}_layer_{}".format(
                load_checkpoint, level) if load_checkpoint else None

            prev_layer = RecurrentEncoder(
                name="{}_layer_{}".format(name, level),
                input_sequence=prev_layer,
                rnn_size=rnn_size,
                rnn_cell=rnn_cell,
                rnn_direction=rnn_direction,
                dropout_keep_prob=dropout_keep_prob,
                save_checkpoint=s_ckp,
                load_checkpoint=l_ckp)

        RecurrentEncoder.__init__(
            self,
            name=name,
            input_sequence=prev_layer,
            rnn_size=rnn_sizes[-1],
            rnn_cell=rnn_cell,
            rnn_direction=rnn_directions[-1],
            dropout_keep_prob=dropout_keep_prob,
            save_checkpoint=save_checkpoint,
            load_checkpoint=load_checkpoint)
    # pylint: enable=too-many-arguments,too-many-locals
