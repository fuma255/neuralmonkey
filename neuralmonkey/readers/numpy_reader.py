from typing import List, Callable, Iterable
import os

from typeguard import check_argument_types
import numpy as np


def single_tensor(files: List[str]) -> np.ndarray:
    """Load a single tensor from a numpy file."""
    check_argument_types()
    if len(files) == 1:
        return np.load(files[0])

    return np.concatenate([np.load(f) for f in files], axis=0)


def from_file_list(prefix: str,
                   shape: List[int],
                   suffix: str = "",
                   default_tensor_name: str = "arr_0") -> Callable:
    """Load a list of numpy arrays from a list of .npz numpy files.

    Args:
        prefix: A common prefix for the files in the list.
        shape: The shape of the numpy arrays stored in the referenced files.
        suffix: An optional suffix that will be appended to each path
        default_tensor_name: Key of the tensors to load from the npz files.

    Returns:
        A generator function that yields the loaded arryas.
    """
    check_argument_types()

    def load(files: List[str]) -> Iterable[np.ndarray]:
        for list_file in files:
            with open(list_file, encoding="utf-8") as f_list:
                for line in f_list:
                    path = os.path.join(prefix, line.rstrip()) + suffix
                    with np.load(path) as npz:
                        arr = npz[default_tensor_name]
                        arr_shape = list(arr.shape)
                        if arr_shape != shape:
                            raise ValueError(
                                "Shapes do not match: expected {}, found {}"
                                .format(shape, arr_shape))
                        yield arr
    return load
