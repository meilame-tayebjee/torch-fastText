"""
Train the fastText model implemented with Pytorch.
"""

import sys
import os
import s3fs
import argparse
from typing import List, Optional, Dict
import pytorch_lightning as pl
import torch
from torch import nn
from torch.optim import Adam, SGD
import torch.nn.functional as F
import pandas as pd
import numpy as np
import random
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
import mlflow
import pyarrow.parquet as pq
import time

from pytorch_lightning.callbacks import (
    EarlyStopping,
    LearningRateMonitor,
    ModelCheckpoint,
)


from config.preprocess import clean_text_feature, clean_and_tokenize_df
from config.dataset import FastTextModelDataset
from tokenizer.tokenizer import NGramTokenizer
from models.model import FastTextModule, FastTextModel
from losses.ova import OneVsAllLoss



def train(
    df: pd.DataFrame,
    y: str,
    text_feature: str,
    categorical_features: Optional[List[str]],
    params: Dict,
    loss: str = "crossentropy",
):
    """
    Train method.

    Args:
        df (pd.DataFrame): Training data.
        y (str): Name of the variable to predict.
        text_feature (str): Name of the text feature.
        categorical_features (Optional[List[str]]): Names
            of the categorical features.
        params (Dict): Parameters for model and training.
    """
    max_epochs = params["max_epochs"]
    patience = params["patience"]
    train_proportion = params["train_proportion"]
    batch_size = params["batch_size"]
    lr = params["lr"]
    buckets = params["buckets"]
    embedding_dim = params["dim"]
    min_count = params["minCount"]
    min_n = params["minn"]
    max_n = params["maxn"]
    word_ngrams = params["wordNgrams"]
    sparse = params["sparse"]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Train/val split
    features = [text_feature]
    if categorical_features is not None:
        features += categorical_features
    X_train, X_val, y_train, y_val = train_test_split(
        df[features],
        df[y],
        test_size=1 - train_proportion,
        random_state=0,
        shuffle=True,
    )

    training_text = X_train[text_feature].to_list()
    tokenizer = NGramTokenizer(min_count, min_n, max_n, buckets, word_ngrams, training_text)

    train_dataset = FastTextModelDataset(
        categorical_variables=[
            X_train[column].to_list() for column in X_train[categorical_features]
        ],
        texts=training_text,
        outputs=y_train.to_list(),
        tokenizer=tokenizer,
    )
    val_dataset = FastTextModelDataset(
        categorical_variables=[X_val[column].to_list() for column in X_val[categorical_features]],
        texts=X_val[text_feature].to_list(),
        outputs=y_val.to_list(),
        tokenizer=tokenizer,
    )
    train_dataloader = train_dataset.create_dataloader(batch_size=batch_size, num_workers=72)
    val_dataloader = val_dataset.create_dataloader(batch_size=batch_size, num_workers=72)

    # Compute num_classes and categorical_vocabulary_sizes
    num_classes = df[y].nunique()
    categorical_vocabulary_sizes = [np.max(X_train[feature]) for feature in categorical_features]
    # Model
    model = FastTextModel(
        tokenizer=tokenizer,
        nace_encoder=encoder,
        embedding_dim=embedding_dim,
        vocab_size=buckets + tokenizer.get_nwords() + 1,
        num_classes=num_classes,
        categorical_vocabulary_sizes=categorical_vocabulary_sizes,
        padding_idx=buckets + tokenizer.get_nwords(),
        sparse=sparse,
    ).to(device)

    # Define optimizer & scheduler
    if sparse:
        optimizer = SGD
    else:
        optimizer = Adam
    optimizer_params = {"lr": lr}
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau
    scheduler_params = {
        "mode": "min",
        "patience": patience,
    }
    loss = nn.CrossEntropyLoss() if loss == "crossentropy" else OneVsAllLoss()
    # Lightning module
    module = FastTextModule(
        model=model,
        loss=loss,
        optimizer=optimizer,
        optimizer_params=optimizer_params,
        scheduler=scheduler,
        scheduler_params=scheduler_params,
        scheduler_interval="epoch",
    )

    # Trainer callbacks
    checkpoints = [
        {
            "monitor": "validation_loss",
            "save_top_k": 1,
            "save_last": False,
            "mode": "min",
        }
    ]
    callbacks = [ModelCheckpoint(**checkpoint) for checkpoint in checkpoints]
    callbacks.append(
        EarlyStopping(
            monitor="validation_loss",
            patience=patience,
            mode="min",
        )
    )
    callbacks.append(LearningRateMonitor(logging_interval="step"))

    # Strategy
    strategy = "auto"

    # Trainer
    trainer = pl.Trainer(
        callbacks=callbacks,
        max_epochs=max_epochs,
        num_sanity_val_steps=2,
        strategy=strategy,
        log_every_n_steps=2,
    )

    # Training
    mlflow.pytorch.autolog()
    torch.cuda.empty_cache()
    torch.set_float32_matmul_precision("medium")
    trainer.fit(module, train_dataloader, val_dataloader)

    return trainer, module


if __name__ == "__main__":
    np.random.seed(0)
    random.seed(0)

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--uri",
        type=str,
        help="MLflow tracking URI",
        default="https://projet-ape-mlflow.user.lab.sspcloud.fr/",
    )
    parser.add_argument("--experiment_name", type=str, help="MLflow experiment name", default=None)
    parser.add_argument("--run_name", type=str, help="MLflow run name", default=None)

    parser.add_argument(
        "--loss",
        type=str,
        help="Loss function",
        default="crossentropy",
        choices=["crossentropy", "ova"],
    )

    args = parser.parse_args()

    remote_server_uri = args.uri
    experiment_name = args.experiment_name
    run_name = args.run_name
    loss = args.loss

    print("GPU available ? ", torch.cuda.is_available())
    # Load data
    print("Loading data...", end="")
    start = time.time()
    fs = s3fs.S3FileSystem(
        client_kwargs={"endpoint_url": "https://minio.lab.sspcloud.fr"},
        key=os.environ["AWS_ACCESS_KEY_ID"],
        secret=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    df = (
        pq.ParquetDataset(
            "projet-ape/extractions/20241027_sirene4.parquet",
            filesystem=fs,
        )
        .read_pandas()
        .to_pandas()
    )
    end = time.time()
    print(f"Data loaded. Time: {round(end-start, 2)} s.")

    # Clean text feature
    print("Cleaning text feature...", end="")
    start = time.time()
    df["libelle"] = clean_text_feature(df["libelle"])
    end = time.time()
    print(f"Text cleaned. Time: {round(end-start, 2)} s.")

    # Encode classes
    encoder = LabelEncoder()
    df["apet_finale"] = encoder.fit_transform(df["apet_finale"])

    print("Tokenizing categorical variables...", end="")
    start = time.time()
    df, les = clean_and_tokenize_df(df)
    end = time.time()
    print(f"Categorical variables handled. Time: {round(end-start, 2)} s.")

    # Start MLflow run
    mlflow.set_tracking_uri(remote_server_uri)
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name=run_name):
        # log les to mlflow
        print("Logging categorical variables mappings...", end="")
        for i, le in enumerate(les):
            mlflow.log_param(f"le_{i}", le.classes_)
        # Train
        print("Done.")
        print("Starting training !")
        trainer, light_module = train(
            df=df,
            y="apet_finale",
            text_feature="libelle",
            categorical_features=["EVT", "CJ", "NAT", "TYP", "CRT", "SRF"],
            params={
                "max_epochs": 1,
                "patience": 3,
                "train_proportion": 0.8,
                "batch_size": 256,
                "lr": 0.004,
                "buckets": 2000000,
                "dim": 180,
                "minCount": 1,
                "minn": 3,
                "maxn": 6,
                "wordNgrams": 3,
                "sparse": False,
            },
            loss=loss,
        )
        best_model = type(light_module).load_from_checkpoint(
            checkpoint_path=trainer.checkpoint_callback.best_model_path,
            model=light_module.model,
            loss=light_module.loss,
            optimizer=light_module.optimizer,
            optimizer_params=light_module.optimizer_params,
            scheduler=light_module.scheduler,
            scheduler_params=light_module.scheduler_params,
            scheduler_interval=light_module.scheduler_interval,
        )
        mlflow.pytorch.log_model(
            artifact_path="model",
            code_paths=["src/models/", "src/config/", "src/explainability/", "src/tokenizer/"],
            pytorch_model=best_model.to("cpu"),
        )
