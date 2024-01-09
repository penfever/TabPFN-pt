import time
from datetime import datetime
import json
import os
import wandb

from scripts.model_builder import get_model, save_model
from scripts.model_configs import *
from priors.utils import uniform_int_sampler_f
from notebook_utils import *
from utils import get_wandb_api_key

import ConfigSpace

def is_json_serializable(obj):
    """
    Test if an object is JSON serializable.

    Args:
    obj (any): The object to test for JSON serialization.

    Returns:
    bool: True if the object is JSON serializable, False otherwise.
    """
    try:
        json.dumps(obj)
        return True
    except (TypeError, OverflowError):
        return False

def make_serializable(config_sample):
    if isinstance(config_sample, torch.Tensor):
        config_sample = "tensor"
    if isinstance(config_sample, dict):
        config_sample = {k: make_serializable(config_sample[k]) for k in config_sample}
    if isinstance(config_sample, list):
        config_sample = [make_serializable(v) for v in config_sample]
    if callable(config_sample):
        config_sample = str(config_sample)
    if not is_json_serializable(config_sample):
        config_sample = str(config_sample)
    return config_sample

def train_function(config_sample, i=0, add_name=''):

    if config_sample['boosting'] or config_sample['rand_init_ensemble'] or config_sample['bagging']:
        #don't save checkpoints for ensembling, just prefixes
        save_every_k = config_sample['epochs'] + 1
    else:
        save_every_k = config_sample['save_every_k_epochs']
    epochs = []

    def save_callback(model, epoch, values_to_log):
        #NOTE: I think the 'epoch' value is actually 1 / config['epochs']
        epochs.append(epoch)
        if not hasattr(model, 'last_saved_epoch'):
            model.last_saved_epoch = 0
        if len(epochs) % save_every_k == 0:
            print('Saving model..')
            config_sample['epoch_in_training'] = epoch
            save_model(model, config_sample['base_path'], f'prior_diff_real_checkpoint{add_name}_n_{i}_epoch_{model.last_saved_epoch}.cpkt',
                           config_sample)
            model.last_saved_epoch = model.last_saved_epoch + 1 # TODO: Rename to checkpoint

    def no_callback(model, epoch, values_to_log):
        pass

    if config_sample['boosting'] or config_sample['rand_init_ensemble'] or config_sample['bagging']:
        my_callback = no_callback
    else:
        my_callback = save_callback

    # todo: get_model shouldn't be the method that trains the model
    model, results_dict = get_model(config_sample
                      , config_sample["device"]
                      , should_train=True
                      , state_dict=config_sample["state_dict"]
                      , epoch_callback = my_callback)
    
    return results_dict

def set_compatibility_params(config, args):
    """
    The parameters listed here either are known to have no effect when using real data priors, or we don't know whether they have an effect.
    """

    # Evaluation parameters from original TabPFN code?

    # config["large_datasets"] = True
    # config["max_samples"] = 10000 if config["large_datasets"] else 5000
    # config["suite"]='cc'

    #Value set to true in the script; seems to have no effect on zs accuracy
    config['recompute_attn'] = True

    #parameters related to synthetic priors
    if args.prior_type == 'prior_bag':
        config['prior_type'], config['differentiable'], config['flexible'] = 'prior_bag', True, True
    else:
        #TODO: check this
        config['prior_type'], config['differentiable'], config['flexible'] = args.prior_type, True, False
    config['output_multiclass_ordered_p'] = 0.
    del config['differentiable_hyperparameters']['output_multiclass_ordered_p']
    config['multiclass_type'] = 'rank'
    del config['differentiable_hyperparameters']['multiclass_type']
    config['sampling'] = 'normal' # vielleicht schlecht?
    del config['differentiable_hyperparameters']['sampling']
    config['pre_sample_causes'] = True
    config['multiclass_loss_type'] = 'nono' # 'compatible'
    config['categorical_feature_p'] = .2 # diff: .0
    config['nan_prob_no_reason'] = .0
    config['nan_prob_unknown_reason'] = .0 # diff: .0
    config['set_value_to_nan'] = .1 # diff: 1.
    config['new_mlp_per_example'] = True
    config['prior_mlp_scale_weights_sqrt'] = True
    config['batch_size_per_gp_sample'] = None
    config['differentiable_hps_as_style'] = False
    config['normalize_ignore_label_too'] = False
    config["mix_activations"] = False # False heisst eig True
    config['multiclass_type'] = config['multiclass_type'] if 'multiclass_type' in config else 'rank'
    config['balanced'] = False

    # ?
    config['canonical_y_encoder'] = False

    # Can't find where in the code where this is used -- would be useful if it worked
    config['total_available_time_in_s'] = None #60*60*22 # 22 hours for some safety...

    # Seems to have no effect on ZS accuracy
    config['efficient_eval_masking'] = True

    return config

def reload_config(config_type='causal', task_type='multiclass', longer=0, args=None):
    config = get_prior_config(config_type=config_type)
        
    #hard-coded limits of original TabPFN model
    config['max_num_classes'] = 10
    config["max_features"] = 100

    #prompt tuning
    config['prompt_tuning'] = args.prompt_tuning
    config['tuned_prompt_size'] = args.tuned_prompt_size
    config['tuned_prompt_label_balance'] = args.tuned_prompt_label_balance

    #eval fit samples and min batches per epoch
    config['num_eval_fitting_samples'] = args.num_eval_fitting_samples
    config['min_batches_per_epoch'] = args.min_batches_per_epoch

    # zs eval parameters
    config['zs_eval_ensemble'] = args.zs_eval_ensemble
    config['random_feature_rotation'] = True if config['zs_eval_ensemble'] > 0 else False
    config['rotate_normalized_labels'] = True if config['zs_eval_ensemble'] > 0 else False

    # core parameters
    config['lr'] = args.lr
    config['early_stopping_patience'] = args.early_stopping
    config['rand_seed'] = args.seed
    config['emsize'] = 512
    config['nhead'] = config['emsize'] // 128
    config['bptt'] = args.bptt
    config['max_eval_pos'] = config['bptt'] - 128
    config['aggregate_k_gradients'] = args.aggregate_k_gradients
    config['epochs'] = args.epochs
    config['warmup_epochs'] = args.epochs // 10

    # data preprocessing
    config['do_preprocess'] = args.do_preprocess
    config['preprocess_type'] = args.preprocess_type
    config['normalize_with_sqrt'] = False
    config['split'] = args.split
    config['pad_features'] = args.pad_features
    config['reseed_data'] = args.reseed_data
    config['normalize_to_ranking'] = False # This should be kept to false, it has learning from the future issues

    #meta-parameters
    config['validation_period'] = args.validation_period
    config['verbose'] = args.verbose
    config['save_every_k_epochs'] = args.save_every_k_epochs

    # concatenation
    config['concat_method'] = args.concat_method

    #amp, cuda, paths
    config["device"] = 'cuda'
    config['data_path'] = args.data_path
    config["base_path"] = args.save_path
    config['train_mixed_precision'] = True

    if args.resume is not None:
        model_state, optimizer_state_load, config_sample_load = torch.load(args.resume, map_location='cpu')
        module_prefix = 'module.'
        config["state_dict"] = {k.replace(module_prefix, ''): v for k, v in model_state.items()}
    else:
        config["state_dict"] = None

    #Boosting parameters
    config['boosting'] = args.boosting
    config['boosting_lr'] = args.ensemble_lr
    config['boosting_n_iters'] = args.ensemble_size
    if config['boosting']:
        config['min_eval_pos'] = config['max_eval_pos'] = config['bptt'] = 1024
        config['aggregate_k_gradients'] = 1
    
    #Ensembling parameters
    config['rand_init_ensemble'] = args.rand_init_ensemble
    config['average_ensemble'] = args.average_ensemble
    config['permute_feature_position_in_ensemble'] = args.permute_feature_position_in_ensemble
    config['keep_topk_ensemble'] = args.keep_topk_ensemble

    #Bagging parameters
    config['bagging'] = args.bagging

    #BPTT and batch size
    config['uniform_bptt'] = args.uniform_bptt
    if config['uniform_bptt']:
        config['bptt_extra_samples'] = 128
        if config['bptt'] <= 128:
            print("Warning: bptt should be >= 128 when using uniform bptt, as 128 samples per batch are reserved for evaluation. Setting bptt to 128.")
            config['bptt'] = 128
    else:
        config['bptt_extra_samples'] = None
    config['eval_positions'] = [int(config['bptt'] * 0.95)] if config['bptt_extra_samples'] is None else [int(config['bptt'])]

    #Feature subset selection
    config['subset_features'] = 100
    config['subset_rows'] = -1
    config['subset_features_method'] = args.feature_subset_method
    config['subset_rows_method'] = 'random'

    # wandb
    # todo: for now, most are hard-coded
    config['wandb_log'] = args.wandb_log
    # config_sample['wandb_name'] = args.wandb_name
    config['wandb_group'] = args.wandb_group
    config['wandb_project'] = args.wandb_project
    config['wandb_entity'] = args.wandb_entity
    config['wandb_log_test_interval'] = args.validation_period

    #batch size parameter doesn't have any effect when using real data prior
    config['batch_size'] = args.batch_size

    config = set_compatibility_params(config, args)
    
    model_string = '_multiclass' + '_'+datetime.now().strftime("%m_%d_%Y_%H_%M_%S")

    config['model_string'] = model_string

    return config, model_string

def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='Train a model.')
    parser.add_argument('--resume', type=str, default="./models_diff/prior_diff_real_checkpoint_n_0_epoch_42.cpkt", help='Path to model checkpoint to resume from.')
    parser.add_argument('--save_path', type=str, default="./logs", help='Path to save new checkpoints.')
    parser.add_argument('--prior_type', type=str, default="real", help='Type of prior to use (real, prior_bag).')
    parser.add_argument('--data_path', type=str, default=".", help='Path to data.')
    parser.add_argument('--prompt_tuning', action='store_true', help='Whether to tune the prompt.')
    parser.add_argument('--tuned_prompt_size', type=int, default=0, help='Size of the tuned prompt.')
    parser.add_argument('--tuned_prompt_label_balance', type=str, default='equal', help='Label balance for the tuned prompt (equal, proportional).')
    parser.add_argument('--lr', type=float, default=0.0001, help='Learning rate.')
    parser.add_argument('--batch_size', type=int, default=4, help='Batch size.')
    parser.add_argument('--bptt', type=int, default=1152, help='Batch per train time.')
    parser.add_argument('--uniform_bptt', action='store_true', help='Whether to use uniform bptt. Note that uniform bptt adds 128 extra samples per batch (for evaluation), so bptt should be >= 128.')
    parser.add_argument('--seed', type=int, default=135798642, help='Random seed.')
    parser.add_argument('--early_stopping', type=int, default=2, help='Patience (for early stopping).')
    parser.add_argument('--epochs', type=int, default=100, help='Number of epochs to train for.')
    parser.add_argument('--num_eval_fitting_samples', type=int, default=1000, help='How many samples from the training set to draw when fitting the eval set.')
    parser.add_argument('--split', type=int, default=0, help='Which split to use (0-9?).')
    parser.add_argument('--boosting', action='store_true', help='Whether to use boosting.')
    parser.add_argument('--bagging', action='store_true', help='Whether to produce a bagged ensemble.')
    parser.add_argument('--rand_init_ensemble', action='store_true', help='Ensemble over random initialization.')
    parser.add_argument('--ensemble_lr', type=float, default=0.5, help='Additive learning factor for boosting / ensembling.')
    parser.add_argument('--ensemble_size', type=int, default=5, help='Number of ensemble members.')
    parser.add_argument('--reseed_data', action='store_true', help='Whether to randomly rotate features, labels and fitting data in the ensemble.')
    parser.add_argument('--aggregate_k_gradients', type=int, default=1, help='How many gradients to aggregate.')
    parser.add_argument('--average_ensemble', action='store_true', help='Whether to average the ensemble.')
    parser.add_argument('--permute_feature_position_in_ensemble', action='store_true', help='Whether to ensemble over feature position permutations.')
    parser.add_argument('--concat_method', type=str, default="", help='concatenation method (duplicate, empty = none)')
    parser.add_argument('--save_every_k_epochs', type=int, default=10, help='How often to save new checkpoints.')
    parser.add_argument('--validation_period', type=int, default=4, help='How often to validate.')
    parser.add_argument('--wandb_name', type=str, default='tabpfn_pt_airlines', help='Name for wandb logging.')
    parser.add_argument('--wandb_log', action='store_true', help='Whether to log to wandb.')
    parser.add_argument('--wandb_group', type=str, default='temp', help='Group for wandb logging.')
    parser.add_argument('--wandb_project', type=str, default='tabpfn-pt', help='Project for wandb logging.')
    parser.add_argument('--wandb_entity', type=str, default='nyu-dice-lab', help='Entity for wandb logging.')
    parser.add_argument('--feature_subset_method', type=str, default='mutual_information', help='Method for feature subset selection ("mutual_information, random, first, pca").')
    parser.add_argument('--pad_features', action='store_true', help='Whether to pad features to the maximum number of features.')
    parser.add_argument('--do_preprocess', action='store_true', help='Whether to add tabpfn-style preprocessing to the data.')
    parser.add_argument('--zs-eval-ensemble', type=int, default=0, help='Whether to do ensembled zero-shot evaluation.')
    parser.add_argument('--min_batches_per_epoch', type=int, default=1, help='Minimum number of batches per epoch.')
    parser.add_argument('--keep_topk_ensemble', type=int, default=0, help='Whether to keep only the top-k ensemble members.')
    parser.add_argument('--preprocess_type', type=str, default='none', help='Type of preprocessing to use (none, power_all, quantile_all, robust_all).')
    parser.add_argument('--optuna_objective', type=str, default='Val_Accuracy', help='Objective for optuna.')
    parser.add_argument('--verbose', action='store_true', help='Whether to print more information during training.')
    args = parser.parse_args()
    return args

def train_loop():
    args = parse_args()

    config, model_string = reload_config(longer=1, args=args)

    #TODO: check whether config_sample should be iterated within train_function
    # config_sample = evaluate_hypers(config, args)

    print("Saving config ...")

    os.mkdir(f'{config["base_path"]}/{model_string}')
    config['base_path'] = f'{config["base_path"]}/{model_string}'
    with open(f'{config["base_path"]}/config_diff_real_{model_string}_n_{0}.json', 'w') as f:
        json.dump(make_serializable(config.copy()), f, indent=4)

    print("Training model ...")

    if config['wandb_log']:
        wandb.login(key=get_wandb_api_key())
        wandb.init(config=config, name=model_string, group=config['wandb_group'],
                project=config['wandb_project'], entity=config['wandb_entity'])

    #clean out optuna params
    for k, v in config.items():
        if isinstance(v, ConfigSpace.hyperparameters.CategoricalHyperparameter):
            config[k] = v.default_value

    results_dict = train_function(config, 0, model_string)

    if config['wandb_log']:
        wandb.finish()

    print("Done")

if __name__ == '__main__':
    import signal
    import sys

    def signal_handler(sig, frame):
        signal.signal(sig, signal.SIG_IGN)
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)

    train_loop()