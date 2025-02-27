import argparse
import gzip
import json
import logging
import mlflow
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
import random

from torch.utils.data import IterableDataset

from sklearn.metrics import balanced_accuracy_score
from torch.utils.data import DataLoader
from tqdm import tqdm, trange

# from .dataset import MeliChallengeDataset
# from .utils import PadSequences


logging.basicConfig(
    format="%(asctime)s: %(levelname)s - %(message)s",
    level=logging.INFO
)

class MeliChallengeDataset(IterableDataset):
    def __init__(self,
                 dataset_path,
                 random_buffer_size=2048):
        assert random_buffer_size > 0
        self.dataset_path = dataset_path
        self.random_buffer_size = random_buffer_size

        with gzip.open(self.dataset_path, "rt") as dataset:
            item = json.loads(next(dataset).strip())
            self.n_labels = item["n_labels"]
            self.dataset_size = item["size"]

    def __len__(self):
        return self.dataset_size

    def __iter__(self):
        try:
            with gzip.open(self.dataset_path, "rt") as dataset:
                shuffle_buffer = []

                for line in dataset:
                    item = json.loads(line.strip())
                    item = {
                        "data": item["data"],
                        "target": item["target"]
                    }

                    if self.random_buffer_size == 1:
                        yield item
                    else:
                        shuffle_buffer.append(item)

                        if len(shuffle_buffer) == self.random_buffer_size:
                            random.shuffle(shuffle_buffer)
                            for item in shuffle_buffer:
                                yield item
                            shuffle_buffer = []

                if len(shuffle_buffer) > 0:
                    random.shuffle(shuffle_buffer)
                    for item in shuffle_buffer:
                        yield item
        except GeneratorExit:
            return

class PadSequences:
    def __init__(self, pad_value=0, max_length=None, min_length=1):
        assert max_length is None or min_length <= max_length
        self.pad_value = pad_value
        self.max_length = max_length
        self.min_length = min_length

    def __call__(self, items):
        data = [item["data"] for item in items]
        target = [item["target"] for item in items]
        seq_lengths = [len(d) for d in data]

        if self.max_length:
            max_length = self.max_length
            seq_lengths = [min(self.max_length, l) for l in seq_lengths]
        else:
            max_length = max(self.min_length, max(seq_lengths))

        data = [d[:l] + [self.pad_value] * (max_length - l)
                for d, l in zip(data, seq_lengths)]

        return {
            "data": torch.LongTensor(data),
            "target": torch.LongTensor(target)
        }



class MLPClassifier(nn.Module):
    # Pytorch Module
    # __init__:defines the structure of the network
    def __init__(self,
                 pretrained_embeddings_path,
                 token_to_index,
                 n_labels,
                 hidden_layers=[256, 128],
                 dropout=0.3,
                 vector_size=300,
                 freeze_embedings=True):
        super().__init__()
        with gzip.open(token_to_index, "rt") as fh:
            token_to_index = json.load(fh)
        embeddings_matrix = torch.randn(len(token_to_index), vector_size)
        embeddings_matrix[0] = torch.zeros(vector_size)
        with gzip.open(pretrained_embeddings_path, "rt") as fh:
            next(fh)
            for line in fh:
                word, vector = line.strip().split(None, 1)
                if word in token_to_index:
                    embeddings_matrix[token_to_index[word]] =\
                        torch.FloatTensor([float(n) for n in vector.split()])
        self.embeddings = nn.Embedding.from_pretrained(embeddings_matrix,
                                                       freeze=freeze_embedings,
                                                       padding_idx=0)
        ## Hidden layers definitions
        ############################
        ## https://pytorch.org/docs/stable/generated/torch.nn.Linear.html
        self.hidden_layers = [
            nn.Linear(vector_size, hidden_layers[0]) # first layer
        ]
        for input_size, output_size in zip(hidden_layers[:-1], hidden_layers[1:]):
            self.hidden_layers.append(
                nn.Linear(input_size, output_size) # intermediate layers if hidden_layers´s size > 2
            )
        self.dropout = dropout # percentage of disabled neurons
        self.hidden_layers = nn.ModuleList(self.hidden_layers) #  last layer
        self.output = nn.Linear(hidden_layers[-1], n_labels) 
        self.vector_size = vector_size
        ############################
    # forward: defines how the network layers interact
    def forward(self, x):
        x = self.embeddings(x)
        x = torch.mean(x, dim=1)
        for layer in self.hidden_layers:
            x = F.relu(layer(x))
            if self.dropout:
                x = F.dropout(x, self.dropout)
        x = self.output(x)
        return x


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-data",
                        help="Path to the the training dataset",
                        required=True)
    parser.add_argument("--token-to-index",
                        help="Path to the the json file that maps tokens to indices",
                        required=True)
    parser.add_argument("--pretrained-embeddings",
                        help="Path to the pretrained embeddings file.",
                        required=True)
    parser.add_argument("--language",
                        help="Language working with",
                        required=True)
    parser.add_argument("--test-data",
                        help="If given, use the test data to perform evaluation.")
    parser.add_argument("--validation-data",
                        help="If given, use the validation data to perform evaluation.")
    parser.add_argument("--embeddings-size",
                        default=300,
                        help="Size of the vectors.",
                        type=int)
    parser.add_argument("--hidden-layers",
                        help="Sizes of the hidden layers of the MLP (can be one or more values)",
                        nargs="+",
                        default=[256, 128],
                        type=int)
    parser.add_argument("--dropout",
                        help="Dropout to apply to each hidden layer",
                        default=0.3,
                        type=float)
    parser.add_argument("--epochs",
                        help="Number of epochs",
                        default=1,
                        type=int)

    args = parser.parse_args()

    pad_sequences = PadSequences(
        pad_value=0,
        max_length=None,
        min_length=1
    )

    logging.info("Building training dataset")
    # An iterable Dataset.
    # All datasets that represent an iterable of data samples should subclass it. 
    # Such form of datasets is particularly useful when data come from a stream.
    # All subclasses should overwrite __iter__(), which would return an iterator of samples in this dataset.
    train_dataset = MeliChallengeDataset(
        dataset_path=args.train_data,
        random_buffer_size=2048  # This can be a hypterparameter
    )
    train_loader = DataLoader(
        train_dataset,              # dataset from which to load the data.
        batch_size=128,             # This can be a hyperparameter # how many samples per batch to load (default: ``1``).
        shuffle=False,              # set to ``True`` to have the data reshuffled at every epoch (default: ``False``).
        collate_fn=pad_sequences,   # merges a list of samples to form a mini-batch of Tensor(s).  Used when using batched loading from a map-style dataset.
        drop_last=False             # set to ``True`` to drop the last incomplete batch, if the dataset size is not divisible by the batch size. 
                                    # If ``False`` and the size of dataset is not divisible by the batch size, then the last batch
                                    # will be smaller. (default: ``False``)
        # num_workers=2             # how many subprocesses to use for data loading. ``0`` means that the data will be loaded in the main process. (default: ``0``)
    )

    if args.validation_data:
        logging.info("Building validation dataset")
        validation_dataset = MeliChallengeDataset(
            dataset_path=args.validation_data,
            random_buffer_size=1
        )
        validation_loader = DataLoader(
            validation_dataset,
            batch_size=128,
            shuffle=False,
            collate_fn=pad_sequences,
            drop_last=False
        )
    else:
        validation_dataset = None
        validation_loader = None

    if args.test_data:
        logging.info("Building test dataset")
        test_dataset = MeliChallengeDataset(
            dataset_path=args.test_data,
            random_buffer_size=1
        )
        test_loader = DataLoader(
            test_dataset,
            batch_size=128,
            shuffle=False,
            collate_fn=pad_sequences,
            drop_last=False
        )
    else:
        test_dataset = None
        test_loader = None

    mlflow.set_experiment(f"diplodatos.{args.language}")

    with mlflow.start_run():
        logging.info("Starting experiment")
        # Log all relevent hyperparameters
        mlflow.log_params({
            "model_type": "Multilayer Perceptron",
            "embeddings": args.pretrained_embeddings,
            "hidden_layers": args.hidden_layers,
            "dropout": args.dropout,
            "embeddings_size": args.embeddings_size,
            "epochs": args.epochs
        })
        device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

        logging.info("Building classifier")
        model = MLPClassifier(
            pretrained_embeddings_path=args.pretrained_embeddings,
            token_to_index=args.token_to_index,
            n_labels=train_dataset.n_labels,
            hidden_layers=args.hidden_layers,
            dropout=args.dropout,
            vector_size=args.embeddings_size,
            freeze_embedings=True  # This can be a hyperparameter
        )
        model = model.to(device)
        # loss function
        # https://pytorch.org/docs/stable/generated/torch.nn.CrossEntropyLoss.html
        loss = nn.CrossEntropyLoss()        
        # optimizer algorithm
        # https://pytorch.org/docs/stable/optim.html
        optimizer = optim.Adam(
            model.parameters(),
            lr=1e-3,           # This can be a hyperparameter
            weight_decay=1e-5  # This can be a hyperparameter # weight for L2 regularization
            # momentum=        # This can be a hyperparameter
        )

        logging.info("Training classifier")
        for epoch in trange(args.epochs):
            model.train()
            running_loss = []
            for idx, batch in enumerate(tqdm(train_loader)):
                # set to zero the parameter gradients
                optimizer.zero_grad()
                # get the inputs; data and target
                data = batch["data"].to(device)
                target = batch["target"].to(device)
                # forward + backward + optimize
                output = model(data) # MLPClassifier
                loss_value = loss(output, target)
                loss_value.backward()
                optimizer.step()
                # statistics
                running_loss.append(loss_value.item())
            mlflow.log_metric("train_loss", sum(running_loss) / len(running_loss), epoch)

            if validation_dataset:
                logging.info("Evaluating model on validation")
                model.eval()
                running_loss = []
                targets = []
                predictions = []
                with torch.no_grad():
                    for batch in tqdm(validation_loader):
                        data = batch["data"].to(device)
                        target = batch["target"].to(device)
                        output = model(data)
                        running_loss.append(
                            loss(output, target).item()
                        )
                        targets.extend(batch["target"].numpy())
                        predictions.extend(output.argmax(axis=1).detach().cpu().numpy())
                    mlflow.log_metric("validation_loss", sum(running_loss) / len(running_loss), epoch)
                    mlflow.log_metric("validation_bacc", balanced_accuracy_score(targets, predictions), epoch)

        if test_dataset:
            logging.info("Evaluating model on test")
            model.eval()
            running_loss = []
            targets = []
            predictions = []
            with torch.no_grad():
                for batch in tqdm(test_loader):
                    data = batch["data"].to(device)
                    target = batch["target"].to(device)
                    output = model(data)
                    running_loss.append(
                        loss(output, target).item()
                    )
                    targets.extend(batch["target"].numpy())
                    predictions.extend(output.argmax(axis=1).detach().cpu().numpy())
                mlflow.log_metric("test_loss", sum(running_loss) / len(running_loss), epoch)
                mlflow.log_metric("test_bacc", balanced_accuracy_score(targets, predictions), epoch)
            

