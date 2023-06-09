# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from .dictionary import Dictionary, TruncatedDictionary

from .fairseq_dataset import FairseqDataset

from .base_wrapper_dataset import BaseWrapperDataset

from .concat_dataset import ConcatDataset
from .id_dataset import IdDataset
from .indexed_dataset import IndexedCachedDataset, IndexedDataset, IndexedRawTextDataset, MMapIndexedDataset
from .language_pair_dataset import LanguagePairDataset
from .lm_context_window_dataset import LMContextWindowDataset
from .distill_dataset import DistillDataset
from .monolingual_dataset import MonolingualDataset
from .strip_token_dataset import StripTokenDataset
from .token_block_dataset import TokenBlockDataset
from .transform_eos_dataset import TransformEosDataset
from .truncate_dataset import TruncateDataset
from .append_token_dataset import AppendTokenDataset

from .iterators import (
    CountingIterator,
    EpochBatchIterator,
    GroupedIterator,
    ShardedIterator,
)

__all__ = [
    'BaseWrapperDataset',
    'ConcatDataset',
    'CountingIterator',
    'Dictionary',
    'EpochBatchIterator',
    'FairseqDataset',
    'GroupedIterator',
    'IdDataset',
    'IndexedCachedDataset',
    'IndexedDataset',
    'IndexedRawTextDataset',
    'LanguagePairDataset',
    'LMContextWindowDataset',
    'DistillDataset',
    'MMapIndexedDataset',
    'MonolingualDataset',
    'ShardedIterator',
    'StripTokenDataset',
    'TokenBlockDataset',
    'TransformEosDataset',
    "TruncateDataset",
    'TruncatedDictionary',
    'AppendTokenDataset',
]
