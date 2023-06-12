import logging
from dataclasses import dataclass
from typing import Optional

import jax
import jmp
import numpy
import tqdm
from transformers import GPT2Tokenizer

import haliax as hax
import levanter
from haliax import Axis
from haliax.partitioning import named_jit, round_axis_for_partitioning
from levanter import callbacks
from levanter.checkpoint import load_checkpoint
from levanter.compat.hf_checkpoints import HFCheckpointConverter, RepoRef
from levanter.config import TrainerConfig
from levanter.data import ReplicatedBatchLoader
from levanter.data.text import LMDatasetConfig, TokenSeqDataset
from levanter.models.gpt2 import Gpt2Config, Gpt2LMHeadModel
from levanter.models.loss import next_token_loss


logger = logging.getLogger(__name__)


@dataclass
class EvalGpt2Config:
    checkpoint_path: Optional[str] = None
    hf_checkpoint: Optional[RepoRef] = None
    trainer: TrainerConfig = TrainerConfig()
    data: LMDatasetConfig = LMDatasetConfig()
    model: Gpt2Config = Gpt2Config()

    compare_torch: bool = False
    eval_on_train: bool = False


@levanter.config.main()
def main(config: EvalGpt2Config):
    config.trainer.initialize(config)
    tokenizer: GPT2Tokenizer = config.data.the_tokenizer

    Batch = Axis("batch", config.trainer.eval_batch_size)

    if config.eval_on_train:
        raw_dataset = TokenSeqDataset(config.data.build_or_load_cache("train"), config.model.Pos)
    else:
        raw_dataset = TokenSeqDataset(config.data.build_or_load_cache("validation"), config.model.Pos)

    eval_loader = ReplicatedBatchLoader(raw_dataset, config.trainer.device_mesh, Batch)

    # some axes we use outside the model proper
    Pos = config.model.Pos
    KeyPos = config.model.Pos

    compute_axis_mapping = config.trainer.compute_axis_mapping
    parameter_axis_mapping = config.trainer.parameter_axis_mapping

    with config.trainer.device_mesh, hax.axis_mapping(parameter_axis_mapping):
        key = jax.random.PRNGKey(0)

        vocab_size = len(tokenizer)
        Vocab = round_axis_for_partitioning(Axis("vocab", vocab_size), compute_axis_mapping)
        if vocab_size != Vocab.size:
            logger.info(f"Rounding vocab size from {vocab_size} to {Vocab.size} for partitioning")

        mp: jmp.Policy = config.trainer.mp

        def compute_loss(model: Gpt2LMHeadModel, input_ids):
            with hax.axis_mapping(compute_axis_mapping):
                model = mp.cast_to_compute(model)
                attn_mask = hax.nn.attention.causal_mask(Pos, KeyPos)
                pred_y = model(input_ids, inference=True, key=None, attn_mask=attn_mask)
                pred_y = mp.cast_to_output(pred_y)

                return hax.mean(next_token_loss(Pos, Vocab, pred_y, input_ids)).scalar()

        compute_loss_pjit = named_jit(
            compute_loss,
            out_axis_resources=compute_axis_mapping,
            axis_resources=compute_axis_mapping,
        )

        total = config.trainer.max_eval_batches

        # initialize the model
        if config.checkpoint_path is not None:

            @named_jit(axis_resources=parameter_axis_mapping)
            def init_model():
                model = Gpt2LMHeadModel.init(Vocab, config.model, key=key)
                model = config.trainer.mp.cast_to_param(model)
                return model

            model = init_model()

            # TODO: switch to throwing instead of returning None
            model, _, _ = load_checkpoint(model, None, config.checkpoint_path)  # type: ignore
            loss = callbacks.eval_loss_loop(compute_loss_pjit, model, eval_loader, max_batches=total)

            del model
            print("Loss from Levanter model: ", loss)

        if config.hf_checkpoint is not None:
            # load the huggingface model
            converter = HFCheckpointConverter(Gpt2Config, config.hf_checkpoint)
            model_from_hf_checkpoint = converter.load_pretrained(Gpt2LMHeadModel, config.hf_checkpoint)
            loss = callbacks.eval_loss_loop(
                compute_loss_pjit, model_from_hf_checkpoint, eval_loader, max_batches=total
            )

            print("Loss from HF model: ", loss)

            if config.compare_torch:
                import torch
                from transformers import GPT2LMHeadModel as TorchGPT2LMHeadModel

                torch_model: TorchGPT2LMHeadModel = TorchGPT2LMHeadModel.from_pretrained(
                    config.hf_checkpoint.model_name_or_path, revision=config.hf_checkpoint.revision
                )
                torch_model.eval()
                torch_model.to("cpu")

                loss = 0.0
                n = 0
                for batch in tqdm.tqdm(eval_loader, total=total, desc="Evaluating (torch)"):
                    torch_ids = torch.from_numpy(numpy.array(batch)).to(torch.int64)
                    with torch.no_grad():
                        loss += torch_model(input_ids=torch_ids, labels=torch_ids)[0].item()
                    n += 1
                    if total is not None and n >= total:
                        break

                print("Loss from Torch model: ", loss / n)


if __name__ == "__main__":
    main()
