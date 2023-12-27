# this script is based on the TabSurvey script and function load_data, which returns a specific dataset (X, y).
# instead, this script "prepares" each dataset implemented in our codebase, by doing the following:
# - reading the dataset from a file or online source
# - applying any necessary cleaning (no pre-processing, like encoding or scaling variables)
# - writes each dataset to its own local directory. each dataset directory will contain:
# -- a compressed version of the dataset (X.npy and y.npy)
# -- a json containing metadata
#
# example use - initialize the CaliforniaHousing dataset, write it to a local folder, and then read it into a new TabularDataset object:
#
# >>> from tabzilla_prepare_data import CaliforniaHousing, TabularDataset
# >>> from pathlib import Path
# >>> c = CaliforniaHousing()
# >>> c.name
# 'CaliforniaHousing'
# >>> p = Path("temp/cal-housing").resolve()
# >>> c.write(p)
# >>> c2 = TabularDataset.read(p)
# >>> c2.name
# 'CaliforniaHousing'


import gzip
import json
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.preprocessing import LabelEncoder

import time

from sklearn.compose import ColumnTransformer
from sklearn.feature_selection import SelectKBest, mutual_info_classif
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, QuantileTransformer, RobustScaler, PowerTransformer

import torch

from tabpfn.utils import normalize_data, to_ranking_low_mem, remove_outliers, NOP, normalize_by_used_features_f

class TabularDataset(object):
    def __init__(
        self,
        name: str,
        X: np.ndarray,
        y: np.ndarray,
        cat_idx: list,
        target_type: str,
        num_classes: int,
        num_features: Optional[int] = None,
        num_instances: Optional[int] = None,
        cat_dims: Optional[list] = None,
        split_indeces: Optional[list] = None,
        split_source: Optional[str] = None,
    ) -> None:
        """
        name: name of the dataset
        X: matrix of shape (num_instances x num_features)
        y: array of length (num_instances)
        cat_idx: indices of categorical features
        target_type: {"regression", "classification", "binary"}
        num_classes: 1 for regression 2 for binary, and >2 for classification
        num_features: number of features
        num_instances: number of instances
        split_indeces: specifies dataset splits as a list of dictionaries, with entries "train", "val", and "test".
            each entry specifies the indeces corresponding to the train, validation, and test set.
        """
        assert isinstance(X, np.ndarray), "X must be an instance of np.ndarray"
        assert isinstance(y, np.ndarray), "y must be an instance of np.ndarray"
        assert (
            X.shape[0] == y.shape[0]
        ), "X and y must match along their 0-th dimensions"
        assert len(X.shape) == 2, "X must be 2-dimensional"
        assert len(y.shape) == 1, "y must be 1-dimensional"

        if num_instances is not None:
            assert (
                X.shape[0] == num_instances
            ), f"first dimension of X must be equal to num_instances. X has shape {X.shape}"
            assert y.shape == (
                num_instances,
            ), f"shape of y must be (num_instances, ). y has shape {y.shape} and num_instances={num_instances}"
        else:
            num_instances = X.shape[0]

        if num_features is not None:
            assert (
                X.shape[1] == num_features
            ), f"second dimension of X must be equal to num_features. X has shape {X.shape}"
        else:
            num_features = X.shape[1]

        if len(cat_idx) > 0:
            assert (
                max(cat_idx) <= num_features - 1
            ), f"max index in cat_idx is {max(cat_idx)}, but num_features is {num_features}"
        assert target_type in ["regression", "classification", "binary"]

        if target_type == "regression":
            assert num_classes == 1
        elif target_type == "binary":
            assert num_classes == 1
        elif target_type == "classification":
            assert num_classes > 2

        self.name = name
        self.X = X
        self.y = y
        self.cat_idx = cat_idx
        self.target_type = target_type
        self.num_classes = num_classes
        self.num_features = num_features
        self.cat_dims = cat_dims
        self.num_instances = num_instances
        self.split_indeces = split_indeces
        self.split_source = split_source

        pass

    def target_encode(self):
        # print("target_encode...")
        le = LabelEncoder()
        self.y = le.fit_transform(self.y)

        # Sanity check
        if self.target_type == "classification":
            assert self.num_classes == len(
                le.classes_
            ), "num_classes was set incorrectly."

    def cat_feature_encode(self):
        # print("cat_feature_encode...")
        if not self.cat_dims is None:
            raise RuntimeError(
                "cat_dims is already set. Categorical feature encoding might be running twice."
            )
        self.cat_dims = []

        # Preprocess data
        for i in range(self.num_features):
            if self.cat_idx and i in self.cat_idx:
                le = LabelEncoder()
                self.X[:, i] = le.fit_transform(self.X[:, i])

                # Setting this?
                self.cat_dims.append(len(le.classes_))

    def get_metadata(self) -> dict:
        return {
            "name": self.name,
            "cat_idx": self.cat_idx,
            "cat_dims": self.cat_dims,
            "target_type": self.target_type,
            "num_classes": self.num_classes,
            "num_features": self.num_features,
            "num_instances": self.num_instances,
            "split_source": self.split_source,
        }

    @classmethod
    def read(cls, p: Path):
        """read a dataset from a folder"""

        # make sure that all required files exist in the directory
        X_path = p.joinpath("X.npy.gz")
        y_path = p.joinpath("y.npy.gz")
        metadata_path = p.joinpath("metadata.json")
        split_indeces_path = p / "split_indeces.npy.gz"

        assert X_path.exists(), f"path to X does not exist: {X_path}"
        assert y_path.exists(), f"path to y does not exist: {y_path}"
        assert (
            metadata_path.exists()
        ), f"path to metadata does not exist: {metadata_path}"
        assert (
            split_indeces_path.exists()
        ), f"path to split indeces does not exist: {split_indeces_path}"

        # read data
        with gzip.GzipFile(X_path, "r") as f:
            X = np.load(f, allow_pickle=True)
        with gzip.GzipFile(y_path, "r") as f:
            y = np.load(f)
        with gzip.GzipFile(split_indeces_path, "rb") as f:
            split_indeces = np.load(f, allow_pickle=True)

        # read metadata
        with open(metadata_path, "r") as f:
            kwargs = json.load(f)

        kwargs["X"], kwargs["y"], kwargs["split_indeces"] = X, y, split_indeces
        return cls(**kwargs)

    def write(self, p: Path, overwrite=False) -> None:
        """write the dataset to a new folder. this folder cannot already exist"""

        if not overwrite:
            assert ~p.exists(), f"the path {p} already exists."

        # create the folder
        p.mkdir(parents=True, exist_ok=overwrite)

        # write data
        with gzip.GzipFile(p.joinpath("X.npy.gz"), "w") as f:
            np.save(f, self.X)
        with gzip.GzipFile(p.joinpath("y.npy.gz"), "w") as f:
            np.save(f, self.y)
        with gzip.GzipFile(p.joinpath("split_indeces.npy.gz"), "wb") as f:
            np.save(f, self.split_indeces)

        # write metadata
        with open(p.joinpath("metadata.json"), "w") as f:
            metadata = self.get_metadata()
            json.dump(self.get_metadata(), f, indent=4)

class SubsetMaker(object):
    def __init__(
        self, subset_features, subset_rows, subset_features_method, subset_rows_method
    ):
        self.subset_features = subset_features
        self.subset_rows = subset_rows
        self.subset_features_method = subset_features_method
        self.subset_rows_method = subset_rows_method
        self.row_selector = None
        self.feature_selector = None

    def random_subset(self, X, y, action=[]):
        if "rows" in action:
            row_indices = np.random.choice(X.shape[0], self.subset_rows, replace=False)
        else:
            row_indices = np.arange(X.shape[0])
        if "features" in action:
            feature_indices = np.random.choice(
                X.shape[1], self.subset_features, replace=False
            )
        else:
            feature_indices = np.arange(X.shape[1])
        return X[row_indices[:, None], feature_indices], y[row_indices]

    def first_subset(self, X, y, action=[]):
        if "rows" in action:
            row_indices = np.arange(self.subset_rows)
        else:
            row_indices = np.arange(X.shape[0])
        if "features" in action:
            feature_indices = np.arange(self.subset_features)
        else:
            feature_indices = np.arange(X.shape[1])
        return X[row_indices[:, None], feature_indices], y[row_indices]

    def mutual_information_subset(self, X, y, action="features", split="train"):
        if split not in ["train", "val", "test"]:
            raise ValueError("split must be 'train', 'val', or 'test'")
        if split == "train":
            # NOTE: we are only fitting on the first split we see to save time here
            if getattr(self, "feature_selector", None) is None:
                print("Fitting mutual information feature selector ...")
                # start the timer
                timer = time.time()
                self.feature_selector = SelectKBest(
                    mutual_info_classif, k=self.subset_features
                )
                X = self.feature_selector.fit_transform(X, y)
                print(
                    f"Done fitting mutual information feature selector in {round(time.time() - timer, 1)} seconds"
                )
            else:
                X = self.feature_selector.transform(X)
            return X, y
        else:
            X = self.feature_selector.transform(X)
            return X, y

    def make_subset(
        self,
        X,
        y,
        split="train",
        seed=0,
    ):
        """
        Make a subset of the data matrix X, with subset_features features and subset_rows rows.
        :param X: data matrix
        :param y: labels
        :param subset_features: number of features to keep
        :param subset_rows: number of rows to keep
        :param subset_features_method: method to use for selecting features
        :param subset_rows_method: method to use for selecting rows
        :return: subset of X, y
        """
        print('setting numpy seed to', seed)
        np.random.seed(seed)

        if X.shape[1] > self.subset_features > 0:
            print(
                f"making {self.subset_features}-sized subset of {X.shape[1]} features ..."
            )
            if self.subset_features_method == "random":
                X, y = self.random_subset(X, y, action=["features"])
            elif self.subset_features_method == "first":
                X, y = self.first_subset(X, y, action=["features"])
            elif self.subset_features_method == "mutual_information":
                X, y = self.mutual_information_subset(
                    X, y, action="features", split=split
                )
            else:
                raise ValueError(
                    f"subset_features_method not recognized: {self.subset_features_method}"
                )
        if X.shape[0] > self.subset_rows > 0:
            print(f"making {self.subset_rows}-sized subset of {X.shape[0]} rows ...")
            if self.subset_rows_method == "random":
                X, y = self.random_subset(X, y, action=["rows"])
            elif self.subset_rows_method == "first":
                X, y = self.first_subset(X, y, action=["rows"])
            else:
                raise ValueError(
                    f"subset_rows_method not recognized: {self.subset_rows_method}"
                )

        return X, y


def process_data(
    dataset,
    train_index,
    val_index,
    test_index,
    verbose=False,
    scaler="None",
    one_hot_encode=False,
    impute=True,
    args=None,
):
    # validate the scaler
    assert scaler in ["None", "Quantile"], f"scaler not recognized: {scaler}"

    if scaler == "Quantile":
        scaler_function = QuantileTransformer(
            n_quantiles=min(len(train_index), 1000)
        )  # use either 1000 quantiles or num. training instances, whichever is smaller

    num_mask = np.ones(dataset.X.shape[1], dtype=int)
    num_mask[dataset.cat_idx] = 0
    # TODO: Remove this assertion after sufficient testing
    assert num_mask.sum() + len(dataset.cat_idx) == dataset.X.shape[1]

    X_train, y_train = dataset.X[train_index], dataset.y[train_index]
    X_val, y_val = dataset.X[val_index], dataset.y[val_index]
    X_test, y_test = dataset.X[test_index], dataset.y[test_index]

    # Impute numerical features
    if impute:
        num_idx = np.where(num_mask)[0]

        # The imputer drops columns that are fully NaN. So, we first identify columns that are fully NaN and set them to
        # zero. This will effectively drop the columns without changing the column indexing and ordering that many of
        # the functions in this repository rely upon.
        fully_nan_num_idcs = np.nonzero(
            (~np.isnan(X_train[:, num_idx].astype("float"))).sum(axis=0) == 0
        )[0]
        if fully_nan_num_idcs.size > 0:
            X_train[:, num_idx[fully_nan_num_idcs]] = 0
            X_val[:, num_idx[fully_nan_num_idcs]] = 0
            X_test[:, num_idx[fully_nan_num_idcs]] = 0

        # Impute numerical features, and pass through the rest
        numeric_transformer = Pipeline(steps=[("imputer", SimpleImputer())])
        preprocessor = ColumnTransformer(
            transformers=[
                ("num", numeric_transformer, num_idx),
                ("pass", "passthrough", dataset.cat_idx),
                # ("cat", categorical_transformer, categorical_features),
            ],
            # remainder="passthrough",
        )
        X_train = preprocessor.fit_transform(X_train)
        X_val = preprocessor.transform(X_val)
        X_test = preprocessor.transform(X_test)

        # Re-order columns (ColumnTransformer permutes them)
        perm_idx = []
        running_num_idx = 0
        running_cat_idx = 0
        for is_num in num_mask:
            if is_num > 0:
                perm_idx.append(running_num_idx)
                running_num_idx += 1
            else:
                perm_idx.append(running_cat_idx + len(num_idx))
                running_cat_idx += 1
        assert running_num_idx == len(num_idx)
        assert running_cat_idx == len(dataset.cat_idx)
        X_train = X_train[:, perm_idx]
        X_val = X_val[:, perm_idx]
        X_test = X_test[:, perm_idx]

    if scaler != "None":
        if verbose:
            print(f"Scaling the data using {scaler}...")
        X_train[:, num_mask] = scaler_function.fit_transform(X_train[:, num_mask])
        X_val[:, num_mask] = scaler_function.transform(X_val[:, num_mask])
        X_test[:, num_mask] = scaler_function.transform(X_test[:, num_mask])

    if one_hot_encode:
        ohe = OneHotEncoder(sparse=False, handle_unknown="ignore")
        new_x1 = ohe.fit_transform(X_train[:, dataset.cat_idx])
        X_train = np.concatenate([new_x1, X_train[:, num_mask]], axis=1)
        new_x1_test = ohe.transform(X_test[:, dataset.cat_idx])
        X_test = np.concatenate([new_x1_test, X_test[:, num_mask]], axis=1)
        new_x1_val = ohe.transform(X_val[:, dataset.cat_idx])
        X_val = np.concatenate([new_x1_val, X_val[:, num_mask]], axis=1)
        if verbose:
            print("New Shape:", X_train.shape)

    # create subset of dataset if needed
    if (
        args is not None
        and (args.subset_features > 0 or args.subset_rows > 0)
        and (
            args.subset_features < args.num_features or args.subset_rows < len(X_train)
        )
    ):
        print(
            f"making subset with {args.subset_features} features and {args.subset_rows} rows..."
        )
        if getattr(dataset, "ssm", None) is None:
            dataset.ssm = SubsetMaker(
                args.subset_features,
                args.subset_rows,
                args.subset_features_method,
                args.subset_rows_method,
            )
        X_train, y_train = dataset.ssm.make_subset(
            X_train,
            y_train,
            split="train",
            seed=dataset.subset_random_seed,
        )
        if args.subset_features < args.num_features:
            X_val, y_val = dataset.ssm.make_subset(
                X_val,
                y_val,
                split="val",
                seed=dataset.subset_random_seed,
            )
            X_test, y_test = dataset.ssm.make_subset(
                X_test,
                y_test,
                split="test",
                seed=dataset.subset_random_seed,
            )
        print("subset created")

    return {
        "data_train": (X_train, y_train),
        "data_val": (X_val, y_val),
        "data_test": (X_test, y_test),
    }

import numpy as np
import pandas as pd
from torch.utils.data import Dataset


def data_split(X, y, nan_mask): # indices
    x_d = {
        'data': X,
        'mask': nan_mask.values
    }

    if x_d['data'].shape != x_d['mask'].shape:
        raise 'Shape of data not same as that of nan mask!'

    y_d = {
        'data': y.reshape(-1, 1)
    }
    return x_d, y_d


def data_prep(X, y):
    temp = pd.DataFrame(X).fillna("MissingValue")
    nan_mask = temp.ne("MissingValue").astype(int)
    X, y = data_split(X, y, nan_mask)
    return X, y


class TabDS(Dataset):
    def preprocess_input(self, eval_xs, preprocess_transform):
        import warnings

        if preprocess_transform != 'none':
            if preprocess_transform == 'power' or preprocess_transform == 'power_all':
                pt = PowerTransformer(standardize=True)
            elif preprocess_transform == 'quantile' or preprocess_transform == 'quantile_all':
                pt = QuantileTransformer(output_distribution='normal')
            elif preprocess_transform == 'robust' or preprocess_transform == 'robust_all':
                pt = RobustScaler(unit_variance=True)
        eval_position = eval_xs.shape[0]
        eval_xs = normalize_data(eval_xs, normalize_positions=eval_position)

        warnings.simplefilter('error')
        if preprocess_transform != 'none':
            eval_xs = eval_xs.cpu().numpy()
            feats = set(range(eval_xs.shape[1]))
            for col in feats:
                try:
                    pt.fit(eval_xs[0:eval_position, col:col + 1])
                    trans = pt.transform(eval_xs[:, col:col + 1])
                    # print(scipy.stats.spearmanr(trans[~np.isnan(eval_xs[:, col:col+1])], eval_xs[:, col:col+1][~np.isnan(eval_xs[:, col:col+1])]))
                    eval_xs[:, col:col + 1] = trans
                except:
                    pass
            eval_xs = torch.tensor(eval_xs).float()
        warnings.simplefilter('default')

        eval_xs = eval_xs.unsqueeze(1)

        eval_xs = remove_outliers(eval_xs, normalize_positions=eval_position)
        # Rescale X
        #hard-coded
        max_features = 100
        eval_xs = normalize_by_used_features_f(eval_xs, eval_xs.shape[-1], max_features,
                                            normalize_with_sqrt=False)
        eval_xs = eval_xs.squeeze(1)
        return eval_xs

    def __init__(self, X, Y, num_features, aggregate_k_gradients=1):
        #convert to tensor
        choices = ['power_all', 'none']
        #pick random choice
        choice = np.random.choice(choices)
        self.X = torch.from_numpy(X.copy().astype(np.float32))
        # self.X = self.preprocess_input(torch.from_numpy(X.copy().astype(np.float32)), choice)
        self.y_float = torch.from_numpy(Y.copy().astype(np.float32))
        if len(self.X.shape) > num_features:
            raise ValueError(f"X.shape[1] = {self.X.shape[1]} > num_features = {num_features}")
        if len(self.X.shape) < num_features:
            # pad with zero features
            self.X = torch.cat([self.X, torch.zeros(self.X.shape[0], num_features - self.X.shape[1])], dim=1)
        self.y = torch.from_numpy(Y.copy().astype(np.int64))
        # if len(self.X[0]) % aggregate_k_gradients != 0:
        #     # trim to multiple of aggregate_k_gradients
        #     self.y = self.y[:-(len(self.y) % aggregate_k_gradients)]
        #     self.X = self.X[:len(self.y), :]
        print(f"TabDS: X.shape = {self.X.shape}, y.shape = {self.y.shape}")

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        #(X,y) data, y target, single_eval_pos
        return tuple([self.X[idx], self.y_float[idx]]), self.y[idx], torch.tensor([])
