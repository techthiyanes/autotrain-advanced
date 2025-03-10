import sys
from functools import partial

import torch
from datasets import load_dataset
from huggingface_hub import HfApi
from loguru import logger
from peft import LoraConfig, get_peft_model, prepare_model_for_int8_training
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    default_data_collator,
)

from autotrain.trainers import utils
from autotrain.trainers.callbacks import LoadBestPeftModelCallback, SavePeftModelCallback


def train(config):
    if isinstance(config, dict):
        config = utils.LLMTrainingParams(**config)

    train_data = load_dataset(
        config.data_path,
        split=config.train_split,
        use_auth_token=config.huggingface_token,
    )
    if config.valid_split is not None:
        valid_data = load_dataset(
            config.data_path,
            split=config.valid_split,
            use_auth_token=config.huggingface_token,
        )

    tokenizer = AutoTokenizer.from_pretrained(
        config.model_name,
        use_auth_token=config.huggingface_token,
        trust_remote_code=True,
    )

    if tokenizer.model_max_length > 2048:
        tokenizer.model_max_length = config.model_max_length

    train_data = utils.process_data(
        data=train_data,
        tokenizer=tokenizer,
        config=config,
    )
    if config.valid_split is not None:
        valid_data = utils.process_data(
            data=valid_data,
            tokenizer=tokenizer,
            config=config,
        )

    model_config = AutoConfig.from_pretrained(
        config.model_name,
        use_auth_token=config.huggingface_token,
        trust_remote_code=True,
    )
    logger.info(model_config)

    if config.use_peft:
        model = AutoModelForCausalLM.from_pretrained(
            config.model_name,
            config=model_config,
            use_auth_token=config.huggingface_token,
            torch_dtype=torch.float16,
            load_in_8bit=config.use_int8,
            device_map="auto",
            trust_remote_code=True,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            config.model_name,
            config=model_config,
            use_auth_token=config.huggingface_token,
            trust_remote_code=True,
        )

    model.resize_token_embeddings(len(tokenizer))

    if config.use_peft:
        if config.use_int8:
            model = prepare_model_for_int8_training(model)
        peft_config = LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=utils.TARGET_MODULES.get(config.model_name),
        )
        model = get_peft_model(model, peft_config)

    if config.block_size == -1:
        config.block_size = None

    if config.block_size is None:
        block_size = tokenizer.model_max_length
        if block_size > 1024:
            logger.warning(
                "The chosen tokenizer supports a `model_max_length` that is longer than the default `block_size` value"
                " of 1024. If you would like to use a longer `block_size` up to `tokenizer.model_max_length` you can"
                " override this default with `--block_size xxx`."
            )
            block_size = 1024
    else:
        if config.block_size > tokenizer.model_max_length:
            logger.warning(
                f"The block_size passed ({config.block_size}) is larger than the maximum length for the model"
                f"({tokenizer.model_max_length}). Using block_size={tokenizer.model_max_length}."
            )
        block_size = min(config.block_size, tokenizer.model_max_length)

    config.block_size = block_size

    logger.info(model)

    tokenize_fn = partial(utils.tokenize, tokenizer=tokenizer, config=config)
    group_texts_fn = partial(utils.group_texts, config=config)

    train_data = train_data.map(
        tokenize_fn,
        batched=True,
        num_proc=1,
        remove_columns=list(train_data.features),
        desc="Running tokenizer on train dataset",
    )

    if config.valid_split is not None:
        valid_data = valid_data.map(
            tokenize_fn,
            batched=True,
            num_proc=1,
            remove_columns=list(valid_data.features),
            desc="Running tokenizer on validation dataset",
        )

    train_data = train_data.map(
        group_texts_fn,
        batched=True,
        num_proc=4,
        desc=f"Grouping texts in chunks of {block_size}",
    )

    if config.valid_split is not None:
        valid_data = valid_data.map(
            group_texts_fn,
            batched=True,
            num_proc=4,
            desc=f"Grouping texts in chunks of {block_size}",
        )

    logger.info("creating trainer")
    # trainer specific
    if config.logging_steps == -1:
        if config.valid_split is not None:
            logging_steps = int(0.2 * len(valid_data) / config.train_batch_size)
        else:
            logging_steps = int(0.2 * len(train_data) / config.train_batch_size)
        if logging_steps == 0:
            logging_steps = 1

    else:
        logging_steps = config.logging_steps

    training_args = dict(
        output_dir=config.project_name,
        per_device_train_batch_size=config.train_batch_size,
        per_device_eval_batch_size=config.eval_batch_size,
        learning_rate=config.learning_rate,
        num_train_epochs=config.num_train_epochs,
        evaluation_strategy=config.evaluation_strategy if config.valid_split is not None else "no",
        logging_steps=logging_steps,
        save_total_limit=config.save_total_limit,
        save_strategy=config.save_strategy,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        report_to="tensorboard",
        auto_find_batch_size=config.auto_find_batch_size,
        lr_scheduler_type=config.scheduler,
        optim=config.optimizer,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        max_grad_norm=config.max_grad_norm,
        fp16=config.fp16,
        push_to_hub=False,
        load_best_model_at_end=True if config.valid_split is not None else False,
    )

    args = TrainingArguments(**training_args)

    callbacks = []
    if config.use_peft:
        callbacks.append(SavePeftModelCallback)
        if config.valid_split is not None:
            callbacks.append(LoadBestPeftModelCallback)

    trainer_args = dict(
        args=args,
        model=model,
    )

    trainer = Trainer(
        **trainer_args,
        train_dataset=train_data,
        eval_dataset=valid_data if config.valid_split is not None else None,
        tokenizer=tokenizer,
        data_collator=default_data_collator,
        callbacks=callbacks,
    )
    model.config.use_cache = False

    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    trainer.train()

    logger.info("Finished training, saving model...")
    trainer.save_model(config.project_name)

    if config.use_peft:
        logger.info("Merging adapter weights...")
        utils.merge_adapter(
            base_model_path=config.model_name,
            target_model_path=config.project_name,
            adapter_path=config.project_name,
        )

    if config.push_to_hub:
        logger.info("Pushing model to hub...")
        api = HfApi()
        api.create_repo(repo_id=config.repo_id, repo_type="model")
        api.upload_folder(folder_path=config.project_name, repo_id=config.repo_id, repo_type="model")


if __name__ == "__main__":
    config = {
        # "model_name": "gpt2",
        "model_name": "Salesforce/xgen-7b-8k-base",
        "data_path": "tatsu-lab/alpaca",
        "push_to_hub": False,
        "project_name": "output",
        "use_peft": True,
    }

    train(config)
