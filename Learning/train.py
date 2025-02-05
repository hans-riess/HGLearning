# 2022/03/28~
# Landon Butler, landonb3@seas.upenn.edu
# Edits by Mikhail Hayhoe, mhayhoe@seas.upenn.edu
# Adapted from code by:
# Fernando Gama, fgama@seas.upenn.edu
# Luana Ruiz, rubruiz@seas.upenn.edu


import argparse

# Example usage
# python train.py -p cfg/aggregationGNNsubgraph.cfg -s
parser = argparse.ArgumentParser(description="Training GNN Models")
parser.add_argument('-p', '--path', dest='path', type=str)
parser.add_argument('-s', '--saveModel', dest='saveModel', action='store_true')
parser.set_defaults(saveModel=False, path='cfg/localGNNCLiqueLine.cfg')
cmd_args = parser.parse_args()

import numpy as np

np.seterr(divide='ignore')
import torch
import json

torch.set_default_dtype(torch.float64)
import torch.nn as nn
import torch.optim as optim
import matplotlib
import matplotlib.pyplot as plt

plt.rcParams["text.usetex"] = False
import warnings

warnings.simplefilter(action='ignore', category=UserWarning)

import pickle
import configparser
import os
from os import path
import datetime
from pathlib import Path
import ast
from mlxtend.plotting import plot_confusion_matrix
import sys
import csv

# \\\ Alelab libraries:

import alegnn.utils.graphML as gml
import alegnn.modules.model as model

# \\\ Separate functions:
from alegnn.utils.miscTools import writeVarValues
from alegnn.utils.miscTools import saveSeed

# Start measuring time
startRunTime = datetime.datetime.now()

# from gnn_data.dataTools import dataMisinformation
sys.path.insert(1, os.path.abspath('../Synthetic_Data_Generation'))
sys.path.insert(1, os.path.abspath('../Data'))
from Source_Localization import hypergraphSources
from DHG_datasets import dhgData
from Hypergraphs import HG_normalized_Laplacian_from_incidence
from architectures import LocalGNNCliqueLine, LocalGNNHGLap, LocalGNNClique, LocalGNNLine
# from learner.aggregationGNN import AggregationGNN_DB
# from learner.subgraphAggregationGNN import SubgraphAggregationGNN
# from learner.trainerMisinformation import TrainerMisinformation
# import learner.evaluatorMisinformation as EvaluateMisinformation
from Helpers import sourceTrainer, sourceEvaluate, dhgTrainer, dhgEvaluate
from copy import deepcopy

possible_gnn_models = ['LocalGNNCliqueLine']
figSize = 7  # Overall size of the figure that contains the plot
lineWidth = 2  # Width of the plot lines
markerShape = 'o'  # Shape of the markers
markerSize = 3  # Size of the markers
xAxisMultiplierTrain = 5  # How many training steps in between those shown in
# the plot, i.e., one training step every xAxisMultiplierTrain is shown.
# xAxisMultiplierValid = 1  # How many validation steps in between those shown,


# same as above.

def train_helper(learner_params, train_params, dataset_params, directory, fold=None):
    save_dir = Path(directory)
    tb_dir = save_dir / 'tb'
    ckpt_dir = save_dir / 'ckpt'
    for d in [save_dir, tb_dir, ckpt_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # \\\ Save seeds for reproducibility
    #    PyTorch seeds
    torchState = torch.get_rng_state()
    torchSeed = torch.initial_seed()
    #   Numpy seeds
    numpyState = np.random.RandomState().get_state()
    #   Collect all random states
    randomStates = [{'module': 'numpy', 'state': numpyState},
                    {'module': 'torch', 'state': torchState, 'seed': torchSeed}]

    #   This list and dictionary follows the format to then be loaded, if needed,
    #   by calling the loadSeed function in Utils.miscTools
    saveSeed(randomStates, save_dir)

    varsFile = os.path.join(save_dir, 'hyperparameters.txt')
    writeVarValues(varsFile, learner_params)
    writeVarValues(varsFile, train_params)
    writeVarValues(varsFile, dataset_params)

    #########
    # GRAPH #
    #########

    with open(dataset_params['matrix_path'] + '_GSOs.pkl', 'rb') as f:
        GSOs = pickle.load(f)
    with open(dataset_params['matrix_path'] + '_incidence_matrices.pkl', 'rb') as f:
        incidence_matrices = pickle.load(f) #works but gives waring about csr_matrix

    ########
    # DATA #
    ########

    useGPU = True  # If true, and GPU is available, use it.

    print('cuda:0' if (torch.cuda.is_available()) else 'cpu')
    print(torch.cuda.device_count())
    with open(dataset_params['data_path'], 'rb') as f:
        data = pickle.load(f)
        # data = torch.load(f,map_location='mps:0', pickle_module=pickle)

    if useGPU and torch.cuda.is_available():
        GSOs = [torch.tensor(X.todense(), device='cuda:0') for X in GSOs]
        incidence_matrices = [torch.tensor(X, device='cuda:0') for X in incidence_matrices]
        data.to('cuda:0')
    elif useGPU and torch.backends.mps.is_available():
        GSOs = [torch.tensor(X.todense(), device='mps:0') for X in GSOs]
        incidence_matrices = [torch.tensor(X, device='mps:0') for X in incidence_matrices]
        data.to('mps:0')
    else:
        GSOs = [torch.tensor(X.todense(), device='cpu') for X in GSOs]
        incidence_matrices = [torch.tensor(X, device='cpu') for X in incidence_matrices]
        data.to('cpu')

    # If we want to do CV, set it up. Ensure we use the same seed for each fold
    if dataset_params['num_folds'] is not None:
        data.set_random_seed(dataset_params['seed'])
        data.cv_initialize_folds(dataset_params['num_folds'])
        data.cv_set_fold(fold)

    ############
    # TRAINING #
    ############

    if train_params['loss_function'] == 'MSE':
        loss_function = nn.MSELoss()
        # Ensure correct datatypes
        data.samples['train']['targets'] = data.samples['train']['targets'].type(torch.DoubleTensor)
        data.samples['valid']['targets'] = data.samples['valid']['targets'].type(torch.DoubleTensor)
        data.samples['test']['targets'] = data.samples['test']['targets'].type(torch.DoubleTensor)
    elif train_params['loss_function'] == 'CE':
        loss_function = nn.CrossEntropyLoss()
        # Ensure correct datatypes
        # data.dataType = torch.long
        # data.samples['train']['targets'] = data.samples['train']['targets'].type(torch.LongTensor)
        # data.samples['valid']['targets'] = data.samples['valid']['targets'].type(torch.LongTensor)
        # data.samples['test']['targets'] = data.samples['test']['targets'].type(torch.LongTensor)
    else:
        raise ValueError('loss function in cfg not available')

    if train_params['nonlinearity'] == 'Tanh':
        nonlinearity = nn.Tanh
    elif train_params['nonlinearity'] == 'Sigmoid':
        nonlinearity = nn.Sigmoid
    elif train_params['nonlinearity'] == 'ReLU':
        nonlinearity = nn.ReLU
    else:
        raise ValueError('nonlinearity in cfg not available')

    if learner_params['pooling_function'] == 'NoPool':
        pooling_function = gml.NoPool
    elif learner_params['pooling_function'] == 'MaxPool':
        pooling_function = nn.MaxPool1d
    else:
        raise ValueError('pooling function in cfg not available')

    if dataset_params['data_type'] == 'sourceLoc':
        trainer = sourceTrainer
        evaluator = sourceEvaluate
    elif dataset_params['data_type'] == 'dhg':
        trainer = dhgTrainer
        evaluator = dhgEvaluate
    else:
        raise ValueError('Data type not recognized')

    assert learner_params['gnn_model'] in possible_gnn_models, (
            'selected GNN model ' + learner_params['gnn_model'] + ' not in ' + str(possible_gnn_models))

    if learner_params['gnn_model'] == 'LocalGNNCliqueLine':
        # \\\ Basic parameters for the Local GNN architecture, with clique expansion
        # and line expansion components

        hParamsLocGNN = {'name': 'LocGNN_CL',
                         'archit': LocalGNNCliqueLine,
                         'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                         'dimSignals': learner_params['dim_features'],
                         'nFilterTaps': learner_params['num_filter_taps'],  # Graph convolutional parameters
                         'bias': learner_params['bias'],
                         'nonlinearity': nonlinearity,  # Nonlinearity
                         'nSelectedNodes': None,
                         'poolingFunction': pooling_function,
                         'poolingSize': learner_params['pooling_size'],
                         # Readout layer: local linear combination of features
                         # layers after the GCN layers (map); this fully connected layer
                         # is applied only at each node, without any further exchanges nor
                         # considering all nodes at once, making the architecture entirely
                         # local.
                         'dimReadout': learner_params['dim_readout'],
                         'GSOs': GSOs,  # Graph structure
                         'incidence_matrices': incidence_matrices,
                         'targets': data.targets,
                         'do_sparse': learner_params['do_sparse']}  # Hyperparameters for the SelectionGNN (selGNN)

        hParamsDict = hParamsLocGNN
    elif learner_params['gnn_model'] == 'LocalGNNHGLap':
        # \\\ Basic parameters for the Local GNN architecture, with clique expansion only

        hParamsLocGNN = {'name': 'LocGNN_HGL',
                         'archit': LocalGNNHGLap,
                         'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                         'dimSignals': learner_params['dim_features'],
                         'nFilterTaps': learner_params['num_filter_taps'],  # Graph convolutional parameters
                         'bias': learner_params['bias'],
                         'nonlinearity': nonlinearity,  # Nonlinearity
                         'nSelectedNodes': None,
                         'poolingFunction': pooling_function,
                         'poolingSize': learner_params['pooling_size'],
                         # Readout layer: local linear combination of features
                         # layers after the GCN layers (map); this fully connected layer
                         # is applied only at each node, without any further exchanges nor
                         # considering all nodes at once, making the architecture entirely
                         # local.
                         'dimReadout': learner_params['dim_readout'],
                         'GSOs': HG_normalized_Laplacian_from_incidence(incidence_matrices),  # Graph structure
                         'incidence_matrices': incidence_matrices,
                         'targets': data.targets}  # Hyperparameters for the SelectionGNN (selGNN)

        hParamsDict = hParamsLocGNN
    elif learner_params['gnn_model'] == 'LocalGNNClique':
        # \\\ Basic parameters for the Local GNN architecture, with clique expansion only

        hParamsLocGNN = {'name': 'LocGNN_C',
                         'archit': LocalGNNClique,
                         'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                         'dimSignals': learner_params['dim_features'],
                         'nFilterTaps': learner_params['num_filter_taps'],  # Graph convolutional parameters
                         'bias': learner_params['bias'],
                         'nonlinearity': nonlinearity,  # Nonlinearity
                         'nSelectedNodes': None,
                         'poolingFunction': pooling_function,
                         'poolingSize': learner_params['pooling_size'],
                         # Readout layer: local linear combination of features
                         # layers after the GCN layers (map); this fully connected layer
                         # is applied only at each node, without any further exchanges nor
                         # considering all nodes at once, making the architecture entirely
                         # local.
                         'dimReadout': learner_params['dim_readout'],
                         'GSOs': [GSOs[0]],  # Graph structure
                         'incidence_matrices': incidence_matrices,
                         'targets': data.targets}  # Hyperparameters for the SelectionGNN (selGNN)

        hParamsDict = hParamsLocGNN
    elif learner_params['gnn_model'] == 'LocalGNNLine':
        # \\\ Basic parameters for the Local GNN architecture, with line graph only

        hParamsLocGNN = {'name': 'LocGNN_L',
                         'archit': LocalGNNLine,
                         'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                         'dimSignals': learner_params['dim_features'],
                         'nFilterTaps': learner_params['num_filter_taps'],  # Graph convolutional parameters
                         'bias': learner_params['bias'],
                         'nonlinearity': nonlinearity,  # Nonlinearity
                         'nSelectedNodes': None,
                         'poolingFunction': pooling_function,
                         'poolingSize': learner_params['pooling_size'],
                         # Readout layer: local linear combination of features
                         # layers after the GCN layers (map); this fully connected layer
                         # is applied only at each node, without any further exchanges nor
                         # considering all nodes at once, making the architecture entirely
                         # local.
                         'dimReadout': learner_params['dim_readout'],
                         'GSOs': [GSOs[1]],  # Graph structure
                         'incidence_matrices': incidence_matrices,
                         'targets': data.targets}  # Hyperparameters for the SelectionGNN (selGNN)

        hParamsDict = hParamsLocGNN
    elif learner_params['gnn_model'] == 'aggregationGNN':
        # \\\ Basic parameters for the Aggregation GNN architecture

        hParamsAggGNN = {'name': 'AggGNN',
                         'archit': LocalGNNCliqueLine,
                         'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                         'dimFeatures': learner_params['dim_features'],
                         'nFilterTaps': learner_params['num_filter_taps'],
                         'bias': learner_params['bias'],
                         'nonlinearity': nonlinearity,
                         'poolingFunction': pooling_function,
                         'poolingSize': learner_params['pooling_size'],
                         'dimReadout': learner_params['dim_readout'],
                         'dimEdgeFeatures': learner_params['dim_edge_features'],
                         'nExchanges': learner_params['num_exchanges'],
                         'timestepDelay': learner_params['time_delay'],
                         'summaryStatistics': learner_params['summary_statistics'],
                         'numDifGSOs': learner_params['num_GSOs'],
                         'interactionEffects': learner_params['interaction_effects']}  # Hyperparameters for the AggregationGNN (aggGNN)

        # Chosen architecture

        # Graph convolutional parameters
        # Nonlinearity
        # is affected by the summary
        # Readout layer: local linear combination of features
        # layers after the GCN layers (map)
        # Graph structure

        hParamsDict = hParamsAggGNN
    elif learner_params['gnn_model'] == 'subAggregationGNN':
        # \\\ Basic parameters for the Subgraph Aggregation GNN architecture

        hParamsSubAggGNN = {'name': 'SubAggGNN',
                            'archit': LocalGNNCliqueLine,
                            'device': 'cuda:0' if (useGPU and torch.cuda.is_available()) else 'cpu',
                            'dimFeatures': learner_params['dim_features'],
                            'nFilterTaps': learner_params['num_filter_taps'],
                            'bias': learner_params['bias'],
                            'nonlinearity': nonlinearity,
                            'poolingFunction': pooling_function,
                            'poolingSize': learner_params['pooling_size'],
                            'dimReadout': learner_params['dim_readout'],
                            'dimEdgeFeatures': learner_params['dim_edge_features'],
                            'nExchanges': learner_params['num_exchanges'],
                            'embeddingPooling': learner_params['embedding_pooling']}  # Hyperparameters for the AggregationGNN (aggGNN)
        # Chosen architecture
        # Graph convolutional parameters
        # Nonlinearity
        # is affected by the summary
        # Readout layer: local linear combination of features
        # layers after the GCN layers (map)
        # Graph structure

        hParamsDict = hParamsSubAggGNN
    else:
        raise ValueError('gnn model in cfg not available')

    #####################################################################
    #                                                                   #
    #                           SETUP                                   #
    #                                                                   #
    #####################################################################

    # \\\ If CUDA is selected, empty cache:
    if useGPU and torch.cuda.is_available():
        torch.cuda.empty_cache()

    # This is the dictionary where we store the models (in a model.Model
    # class).
    modelsGNN = {}

    # If a new model is to be created, it should be called for here.
    print()
    print("Initializing model...", flush=True)

    # Now, this dictionary has all the hyperparameters that we need to pass
    # to the architecture, but it also has the 'name' and 'archit' that
    # we do not need to pass them. So we are going to get them out of
    # the dictionary
    thisName = hParamsDict.pop('name')
    callArchit = hParamsDict.pop('archit')
    thisDevice = hParamsDict.pop('device')

    ##############
    # PARAMETERS #
    ##############

    ################
    # ARCHITECTURE #
    ################
    thisArchit = callArchit(**deepcopy(hParamsDict))
    thisArchit.to(thisDevice)

    #############
    # OPTIMIZER #
    #############
    if train_params['optim_alg'] == 'ADAM':
        thisOptim = optim.Adam(thisArchit.parameters(),
                               lr=train_params['learning_rate'],
                               betas=(train_params['beta2'], train_params['beta1']))
    elif train_params['optim_alg'] == 'SGD':
        thisOptim = optim.SGD(thisArchit.parameters(),
                              lr=train_params['learning_rate'])
    else:
        raise ValueError('optimization algorithm in cfg not available')

    ########
    # LOSS #
    ########
    thisLossFunction = loss_function

    ###########
    # TRAINER #
    ###########
    thisTrainer = trainer

    #############
    # EVALUATOR #
    #############
    thisEvaluator = evaluator

    #########
    # MODEL #
    #########
    modelCreated = model.Model(thisArchit, thisLossFunction, thisOptim, thisTrainer, thisEvaluator, thisDevice,
                               thisName, save_dir)
    modelsGNN[thisName] = modelCreated

    ############
    # TRAINING #
    ############
    print()
    print("Training model %s..." % thisName)

    if train_params['lr_decay']:
        thisTrainVars = modelsGNN[thisName].train(data, train_params['n_epochs'], train_params['batch_size'],
                                                  validationInterval=train_params['validation_interval'],
                                                  printInterval=train_params['print_interval'],
                                                  learningRateDecayRate=train_params['lr_decay_rate'],
                                                  learningRateDecayPeriod=train_params['lr_decay_period'],
                                                  integral_lipschitz=train_params['integral_lipschitz_constant'])
    else:
        thisTrainVars = modelsGNN[thisName].train(data, train_params['n_epochs'], train_params['batch_size'],
                                                  validationInterval=train_params['validation_interval'],
                                                  printInterval=train_params['print_interval'],
                                                  integral_lipschitz=train_params['integral_lipschitz_constant'])

    ###########
    # TESTING #
    ###########
    print()
    print("Evaluating model %s..." % thisName)
    thisTestVars = modelsGNN[thisName].evaluate(data)

    writeVarValues(varsFile,
                   {'costBestl%s%03dR%02d' % \
                    (thisName, 1, 1): thisTestVars['costBest'],
                    'costLast%s%03dR%02d' % \
                    (thisName, 1, 1): thisTestVars['costLast']})

    if cmd_args.saveModel:
        print()
        print("Saving model...")
        model_save_file = save_dir / 'savedModels' / 'completeModel.pkl'
        with open(model_save_file, 'wb') as handle:
            pickle.dump(modelsGNN[thisName], handle, protocol=pickle.HIGHEST_PROTOCOL)
        print("Model saved at " + str(save_dir))

    print()
    print("Training complete!")
    return thisTrainVars, thisTestVars


def create_plots(save_dir, trainVars, testVars):
    fig = plt.figure(figsize=(3.22 * figSize, 2.4 * figSize))
    xAxisMultiplierValid = trainVars['validationInterval'] / trainVars['nBatches']
    xTrain = np.arange(0, trainVars['nEpochs'] * trainVars['nBatches'], xAxisMultiplierTrain)
    xValid = np.arange(0, trainVars['nEpochs'] * trainVars['nBatches'], trainVars['validationInterval'])
    xValidEpochs = np.arange(0, trainVars['nEpochs'], xAxisMultiplierValid)

    xEpochs = np.arange(0, trainVars['nEpochs'])
    # xValidEpochs = np.append(xValidEpochs, xEpochs[-1])
    lossTrainPlot = trainVars['lossTrain'][xTrain]
    # selectSamplesValid = np.arange(0, len(trainVars['lossValid']), xAxisMultiplierValid)
    lossValidPlot = trainVars['lossValid']
    lossValidStdPlot = trainVars['lossValid_std']
    plt.subplots_adjust(wspace=0.35, hspace=0.25)
    sub1 = fig.add_subplot(3, 4, (1, 2))
    sub2 = fig.add_subplot(3, 4, (3, 4))
    sub3 = fig.add_subplot(3, 4, (5, 6))
    sub4 = fig.add_subplot(3, 4, (7, 8))
    sub5 = fig.add_subplot(3, 4, (9, 10))
    sub6 = fig.add_subplot(3, 4, (11, 12))

    ##########################
    # Loss vs Training Steps #
    ##########################
    sub1.plot(xTrain, lossTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub1.plot(xValid, lossValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub1.set_ylabel(r'Loss')
    sub1.set_xlabel(r'Training Steps')
    sub1.legend([r'Training', r'Validation'])
    sub1.set_title(r'Loss vs. Training Steps')

    ##################
    # Loss vs Epochs #
    ##################
    lossEpochTrainPlot = np.mean(trainVars['lossTrain'].reshape(trainVars['nEpochs'], trainVars['nBatches']), axis=1)
    lossEpochTrainStdPlot = np.mean(trainVars['lossTrain_std'].reshape(trainVars['nEpochs'], trainVars['nBatches']), axis=1)
    sub2.plot(xEpochs, lossEpochTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub2.fill_between(xEpochs, lossEpochTrainPlot + lossEpochTrainStdPlot, lossEpochTrainPlot - lossEpochTrainStdPlot,
                      color='#01256E', linewidth=lineWidth, alpha=0.3)
    sub2.plot(xValidEpochs, lossValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub2.fill_between(xValidEpochs, lossValidPlot + lossValidStdPlot, lossValidPlot - lossValidStdPlot,
              color='#A1CAF1', linewidth=lineWidth, alpha=0.3)
    sub2.set_ylabel(r'Loss')
    sub2.set_xlabel(r'Epochs')
    sub2.legend([r'Training', r'Validation'])
    sub2.set_title(r'Loss vs. Epochs')

    ##########################
    # Eval vs Training Steps #
    ##########################
    costTrainPlot = trainVars['costTrain'][xTrain]
    costValidPlot = trainVars['costValid']
    sub3.plot(xTrain, costTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub3.plot(xValid, costValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub3.plot([0, xTrain[-1]], 2 * [testVars['costBest']],
              color='#9E5B2D', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub3.plot([0, xTrain[-1]], 2 * [testVars['costLast']],
              color='#464646', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub3.set_ylabel(r'Micro-F1')
    sub3.set_xlabel(r'Training Steps')
    sub3.legend([r'Training', r'Validation', r'Eval Best Model', r'Eval Last Model'])
    sub3.set_title(r'Micro-F1 vs. Training Steps (Best: {:.3f})'.format(testVars['costBest']))

    ##################
    # Eval vs Epochs #
    ##################
    costEpochTrainPlot = np.mean(trainVars['costTrain'].reshape(trainVars['nEpochs'], trainVars['nBatches']), axis=1)
    costEpochTrainStdPlot = np.mean(trainVars['costTrain_std'].reshape(trainVars['nEpochs'], trainVars['nBatches']), axis=1)
    costValidStdPlot = trainVars['costValid_std']
    sub4.plot(xEpochs, costEpochTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub4.fill_between(xEpochs, costEpochTrainPlot + costEpochTrainStdPlot, costEpochTrainPlot - costEpochTrainStdPlot,
              color='#01256E', linewidth=lineWidth, alpha=0.3)
    sub4.plot(xValidEpochs, costValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub4.fill_between(xValidEpochs, costValidPlot + costValidStdPlot, costValidPlot - costValidStdPlot,
              color='#A1CAF1', linewidth=lineWidth, alpha=0.3)
    sub4.plot([0, xEpochs[-1]], 2 * [testVars['costBest']],
              color='#9E5B2D', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub4.plot([0, xEpochs[-1]], 2 * [testVars['costLast']],
              color='#464646', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub4.set_ylabel(r'Micro-F1')
    sub4.set_xlabel(r'Epochs')
    sub4.legend([r'Training', r'Validation', r'Eval Best Model', r'Eval Last Model'])
    sub4.set_title(r'Micro-F1 vs. Epochs (Best: {:.3f})'.format(testVars['costBest']))


    '''
    misclassTrainPlot = trainVars['misclassTrain'][xTrain]
    misclassValidPlot = trainVars['misclassValid'][selectSamplesValid]
    sub3.plot(xTrain, misclassTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub3.plot(xValid, misclassValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub3.plot([0, xTrain[-1]], 2 * [testVars['misclassRateBest']],
              color='#9E5B2D', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub3.plot([0, xTrain[-1]], 2 * [testVars['misclassRateLast']],
              color='#464646', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub3.set_ylabel(r'Misclassification Rate')
    sub3.set_xlabel(r'Training Steps')
    sub3.legend([r'Training', r'Validation', r'Eval Best Model', r'Eval Last Model'])
    sub3.set_title(r'Misclassification Rate vs. Training Steps')

    misclassEpochTrainPlot = np.mean(trainVars['misclassTrain'].reshape(trainVars['nEpochs'], trainVars['nBatches']),
                                     axis=1)
    misclassValidPlot = trainVars['misclassValid'][selectSamplesValid]
    sub4.plot(xEpochs, misclassEpochTrainPlot,
              color='#01256E', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub4.plot(xValidEpochs, misclassValidPlot,
              color='#A1CAF1', linewidth=lineWidth,
              marker=markerShape, markersize=markerSize)
    sub4.plot([0, xEpochs[-1]], 2 * [testVars['misclassRateBest']],
              color='#9E5B2D', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub4.plot([0, xEpochs[-1]], 2 * [testVars['misclassRateLast']],
              color='#464646', linewidth=lineWidth,
              linestyle='-.',
              marker=markerShape, markersize=markerSize)
    sub4.set_ylabel(r'Misclassification Rate')
    sub4.set_xlabel(r'Epochs')
    sub4.legend([r'Training', r'Validation', r'Eval Best Model', r'Eval Last Model'])
    sub4.set_title(r'Misclassification Rate vs. Epochs')
    
    labels = ['', 'Fake Posts', 'Real Posts', '']
    '''

    ###########################
    # Confusion Matrix - Best #
    ###########################
    plot_confusion_matrix(conf_mat=np.array(testVars['confusionMatrixBest'],dtype=int), figure=fig, axis=sub5)
    # UserWarning is suppressed
    # sub5.set_xticklabels(labels)
    # sub5.set_yticklabels(labels)
    sub5.set_xlabel(r'Predicted Label')
    sub5.set_ylabel(r'True Label')
    sub5.set_title(r'Confusion Matrix - Best Model')

    ###########################
    # Confusion Matrix - Last #
    ###########################
    plot_confusion_matrix(conf_mat=np.array(testVars['confusionMatrixLast'], dtype=int), figure=fig, axis=sub6)
    # UserWarning is suppressed
    # sub6.set_xticklabels(labels)
    # sub6.set_yticklabels(labels)
    sub6.set_xlabel(r'Predicted Label')
    sub6.set_ylabel(r'True Label')
    sub6.set_title(r'Confusion Matrix - Last Model')

    fig.savefig(os.path.join(save_dir, 'figs.png'), dpi=200)


def run_experiment(args, section_name='', fold=None):
    train_params = {
        'n_epochs': args.getint('n_epochs', 50),
        'batch_size': args.getint('batch_size', 20),
        'integral_lipschitz_constant': args.getfloat('integral_lipschitz_constant', None),
        'learning_rate': args.getfloat('learning_rate', 0.05),
        'loss_function': args.get('loss_function', 'MSE'),
        'nonlinearity': args.get('nonlinearity', 'Sigmoid'),
        'optim_alg': args.get('optim_alg', 'ADAM'),
        'beta1': args.getfloat('beta1', 0.9),
        'beta2': args.getfloat('beta2', 0.999),
        'lr_decay': args.getboolean('lr_decay', False),
        'lr_decay_rate': args.getfloat('lr_decay_rate', 0.9),
        'lr_decay_period': args.getint('lr_decay_period', 1),
        'validation_interval': args.getint('validation_interval', 5),
        'print_interval': args.getint('print_interval', 5)
    }

    learner_params = {
        'gnn_model': args.get('gnn_model', 'LocalGNNCliqueLine'),
        'dim_features': ast.literal_eval(args.get('dim_features', '[1]')),
        'num_filter_taps': ast.literal_eval(args.get('num_filter_taps', '[]')),
        'bias': args.getboolean('bias', False),
        'pooling_function': args.get('pooling_function', 'NoPool'),
        'pooling_size': ast.literal_eval(args.get('pooling_size', '[]')),
        'dim_readout': ast.literal_eval(args.get('dim_readout', '[8,1]')),
        'dim_edge_features': args.getint('dim_edge_features', '1'),
        'time_delay': args.getint('time_delay', '6'),
        'num_exchanges': args.getint('num_exchanges', '3'),
        'num_GSOs': args.getint('num_GSOs', '1'),
        'do_sparse': args.getboolean('do_sparse', False),
        'interaction_effects': args.getboolean('interaction_effects', False),
        'summary_statistics': ast.literal_eval(args.get('summary_statistics', "['mean']")),
        'embedding_pooling': ast.literal_eval(args.get('embedding_pooling', "['mean']"))
    }

    dataset_params = {
        'data_type': args.get('data_type', 'sourceLoc'),
        'matrix_path': args.get('matrix_path', '../data/sourceLoc/sourceLoc'),
        'data_path': args.get('data_path', '../data/sourceLoc/sourceLoc_data.pkl'),
        'num_folds': args.get('num_folds', None),
        'normalize_graph_signal': args.getboolean('normalize_graph_signal', False),
        'prop_data_train': args.getfloat('prop_data_train', 0.6),
        'prop_data_valid': args.getfloat('prop_data_valid', 0.2),
        'prop_data_test': args.getfloat('prop_data_test', 0.2),
        'seed': args.getint('seed', 0),
        'balance_classes': args.getboolean('balance_classes', False),
    }

    today = datetime.datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    directory = Path('models/' + args.get('name', 'localGNNCliqueLine') + '/' + today + section_name)

    trainVars, testVars = train_helper(
        learner_params=learner_params,
        train_params=train_params,
        dataset_params=dataset_params,
        directory=directory,
        fold=fold)

    return trainVars, testVars


# Run multiple experiments, one per fold.
def run_CV(args, section_name=''):
    # Get the number of folds for cross-validation
    num_folds = args.getint('num_folds', None)

    if num_folds is None:
        thisTrainVars, thisTestVars = run_experiment(args, section_name, fold=None)
    else:
        trainVarsCV = []
        testVarsCV = []

        for k in range(num_folds):
            trainVarsExperiment, testVarsExperiment = run_experiment(args, section_name, fold=k)
            trainVarsCV.append(trainVarsExperiment)
            testVarsCV.append(testVarsExperiment)

        thisTrainVars = trainVarsCV[0]
        thisTestVars = testVarsCV[0]

        # Average across the cross-validation folds
        train_keys = ['lossTrain', 'costTrain', 'lossValid', 'costValid', 'costValidBest']
        test_keys = ['costBest', 'costLast', 'confusionMatrixBest', 'confusionMatrixLast']

        for key_name in train_keys:
            stacked_vars = np.stack([trainVarsCV[k][key_name] for k in range(num_folds)])
            thisTrainVars[key_name] = np.mean(stacked_vars, axis=0)
            thisTrainVars[key_name + '_std'] = np.std(stacked_vars, axis=0)

        for key_name in test_keys:
            stacked_vars = np.stack([testVarsCV[k][key_name] for k in range(num_folds)])
            thisTestVars[key_name] = np.mean(stacked_vars, axis=0)
            thisTestVars[key_name + '_std'] = np.std(stacked_vars, axis=0)

        # Take maximum for integral Lipschitz constant
        if thisTestVars['IL_constant_best'] is not None:
            thisTestVars['IL_constant_best'] = [testVarsCV[k]['IL_constant_best'] for k in range(num_folds)]

    # Plot results
    print()
    print("Saving figures...")
    today = datetime.datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    directory = Path('models/' + args.get('name', 'localGNNCliqueLine') + '/' + today + section_name + '_CV')
    os.mkdir(directory)
    create_plots(directory, thisTrainVars, thisTestVars)

    # Return the training and testing variables and performance
    return thisTrainVars, thisTestVars


# Take the output from training and write to a single CSV
def summarize_cv(cvParams):
    name = cvParams[list(cvParams.keys())[0]]['config'].get('name', 'localGNNCliqueLine')
    today = datetime.datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    directory = 'models/' + name + '/' + today + '_CV_summary'
    os.mkdir(directory)

    with open(directory + '/CV.csv', 'w', newline='') as csvfile:
        writer = csv.writer(csvfile, delimiter=',')
        writer.writerow(['Name', 'Train Cost', 'Validation Cost', 'Validation Mean',
                         'Validation Std', 'UCB', 'LCB', 'Test Mean', 'Test Std', '# Epochs',
                         'LR', 'LR Decay Rate', 'LR Decay Period', '# Features', '# Filter Taps', 'Readout',
                         'C_real', 'C_limit'])

        for section_name, vars in cvParams.items():
            trainVars = vars['trainVars']
            testVars = vars['testVars']
            config = vars['config']
            writer.writerow([config.get('gnn_model'), trainVars['costTrain'], trainVars['costValid'],
                             trainVars['costValidBest'], trainVars['costValidBest_std'],
                             trainVars['costValidBest'] + trainVars['costValidBest_std'],
                             trainVars['costValidBest'] - trainVars['costValidBest_std'],
                             testVars['costBest'], testVars['costBest_std'],
                             config.get('n_epochs'), config.get('learning_rate'), config.get('lr_decay_rate'),
                             config.get('lr_decay_period'), config.get('dim_features'), config.get('num_filter_taps'),
                             config.get('dim_readout'), testVars['IL_constant_best'],
                             config.get('integral_lipschitz_constant')])


def main():
    fname = cmd_args.path
    config_file = path.join(path.dirname(__file__), fname)
    config = configparser.ConfigParser()
    config.read(config_file)
    today = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    cvParams = {}

    if config.sections():
        for section_name in config.sections():
            trainVars, testVars = run_CV(config[section_name], section_name)
            cvParams[section_name] = {'trainVars': trainVars, 'testVars': testVars, 'config': config[section_name]}
        summarize_cv(cvParams)
    else:
        trainVars, testVars = run_experiment(config[config.default_section])


if __name__ == '__main__':
    main()
    