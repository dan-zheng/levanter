import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Optional

import equinox as eqx
import jax
from jax.sharding import Mesh

from haliax import Axis

import levanter
from levanter.checkpoint import load_checkpoint
from levanter.compat.hf_checkpoints import RepoRef, load_tokenizer
from levanter.models.gpt2 import Gpt2Config
from levanter.models.lm_model import LmConfig, LmHeadModel
from levanter.utils.jax_utils import use_cpu_device


logger = logging.getLogger(__name__)


@dataclass
class ConvertLmConfig:
    checkpoint_path: str
    output_dir: str
    upload_to_hf: Optional[RepoRef] = None  # if specified, attempt to upload this checkpoint to the hf hub

    model: LmConfig = Gpt2Config()
    save_tokenizer: bool = True  # if True, save the tokenizer to the output directory
    tokenizer: str = "gpt2"
    override_vocab_size: Optional[int] = None  # if specified, override the vocab size in the config

    config_overrides: Optional[dict] = None  # if specified, override the config with these values

    @cached_property
    def the_tokenizer(self):
        return load_tokenizer(self.tokenizer)


def main(config: ConvertLmConfig):
    logger.setLevel(logging.INFO)
    tokenizer = config.the_tokenizer

    vocab_size = config.override_vocab_size or len(tokenizer)
    Vocab = Axis("vocab", vocab_size)

    key = jax.random.PRNGKey(0)

    with use_cpu_device(), Mesh([jax.local_devices(backend="cpu")[0]], "dev"):
        model: LmHeadModel = eqx.filter_eval_shape(config.model.build, Vocab, key=key)
        # TODO: don't load the entire checkpoint into CPU memory when we only need our share of the model
        ckpt = load_checkpoint(model, None, config.checkpoint_path)

        assert ckpt is not None
        model, _, _ = ckpt

        if config.override_vocab_size:
            model = model.resize_vocab(config.override_vocab_size)

        converter = model.config.default_hf_checkpoint_converter.replaced(tokenizer=tokenizer)

        converter.save_pretrained(
            model,
            config.output_dir,
            upload_to_hf=config.upload_to_hf or False,
            save_tokenizer=config.save_tokenizer,
        )


if __name__ == "__main__":
    levanter.config.main(main)()
