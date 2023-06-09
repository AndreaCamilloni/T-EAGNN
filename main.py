from math import ceil
import json
import os
import sys
import random

import matplotlib.pyplot as plt
import numpy as np
from sklearn.metrics import roc_auc_score, roc_curve, classification_report
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

from datasets import link_prediction
from layers import MeanAggregator, LSTMAggregator, MaxPoolAggregator, MeanPoolAggregator
import models
from models import DGNN, AAGNN, EGNNC
from models_variants import EAAGNN, EAACGNN
import utils

random.seed(0)

def main():

    # Set up arguments for datasets, models and training.
    config = utils.parse_args()
    config['num_layers'] = len(config['hidden_dims']) + 1

    if config['cuda'] and torch.cuda.is_available():
        device = 'cuda:0'
    else:
        device = 'cpu'
    config['device'] = device

    # Get the dataset, dataloader and model.
    if not config['val'] and not config['test']:
        dataset_args = ('train', config['num_layers'])

    if config['val']:
        dataset_args = ('val', config['num_layers'])

    if config['test']:
        dataset_args = ('test', config['num_layers'])
    
    datasets = utils.get_dataset_gcn(dataset_args, config['dataset_folder'], is_debug=config["is_debug"])

    
    loaders = []
    for i in range(len(datasets)):
        loaders.append(DataLoader(dataset=datasets[i], batch_size=config['batch_size'],
                        shuffle=True, collate_fn=datasets[i].collate_wrapper))

    print('datasets: ',datasets)
    input_dim, hidden_dim,  output_dim = datasets[0].get_dims()[0], config['hidden_dims'][0], config['out_dim']

    if config["gnn_type"] == "dgnn":
        channel_dim = 1
        # model = DGNN(input_dim, hidden_dim, output_dim,
        #             config['dropout'], config['device'])
        model = EGNNC(input_dim, hidden_dim, output_dim,
                    channel_dim, config['dropout'], config['device'])

    if config["gnn_type"] == "egnnc":
        channel_dim = datasets[0].get_channel()
        model = EGNNC(input_dim, hidden_dim, output_dim,
                    channel_dim, config['dropout'], config['device'])
        
    
    model.to(config['device'])

    if config["classifier"] == "mlp":
        mlp = models.MLPTwoLayers(input_dim=channel_dim*output_dim*2, hidden_dim=output_dim*2, output_dim=1, dropout=0.5)
        mlp.to(config["device"])

    if config["classifier"] == "mlp_tri":
        mlp = models.MLPTwoLayers(input_dim=channel_dim*output_dim*4, hidden_dim=output_dim*2, output_dim=1, dropout=0.5)
        mlp.to(config["device"])

    if config["classifier"] == "mlp_bi_ntn":
        ntn_embedding_size = 64
        mlp = models.MLPTwoLayers(input_dim=channel_dim*output_dim*2 + ntn_embedding_size, hidden_dim=output_dim*2, output_dim=1, dropout=0.5)
        mlp.to(config["device"])             

    if config["classifier"] == "cnn":
        cnn = models.TriangularMotifsCNN(num_channels = 4)
        cnn.to(config["device"])
    
    if config["combined_model"]:
        classifier = mlp if config["classifier"] == "mlp" else cnn
        combined_model = models.CombinedModel(model, classifier, config["classifier"], config["device"])
        combined_model.to(config["device"])


    if config['val'] or config['test']:
        directory = config['saved_models_dir']
        fname = utils.get_fname(config)
        path = os.path.join(directory, fname)
        if config["combined_model"]:
            combined_model.load_state_dict(torch.load(path, map_location=torch.device(device)))
        else:

            if config["classifier"] == "mlp":
                fnamemlp = 'mlp' + fname
                pathmlp = os.path.join(directory, fnamemlp)
                mlp.load_state_dict(torch.load(pathmlp, map_location=torch.device(device)))

            if config["classifier"] == "mlp_tri":
                fnamemlp = 'mlp_tri' + fname
                pathmlp = os.path.join(directory, fnamemlp)
                mlp.load_state_dict(torch.load(pathmlp, map_location=torch.device(device)))

            if config["classifier"] == "mlp_bi_ntn":
                fnamemlp = 'mlp_bi_ntn' + fname
                pathmlp = os.path.join(directory, fnamemlp)
                mlp.load_state_dict(torch.load(pathmlp, map_location=torch.device(device)))
                
            if config["classifier"] == "cnn":
                fnamemlp = 'cnn' + fname
                pathmlp = os.path.join(directory, fnamemlp)
                cnn.load_state_dict(torch.load(pathmlp, map_location=torch.device(device)))

    print(model)

    stats_per_batch = config['stats_per_batch']

    criterion = utils.get_criterion(config['task'])

    sigmoid = nn.Sigmoid()

    # =================== training ===================
    # Compute ROC-AUC score for the untrained model.
    if not config['val'] and not config['test']:
        print('--------------------------------')
        print('Computing ROC-AUC score for the training dataset before training.')
        y_true, y_scores = [], []
        for i in range(len(datasets)):
            num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
            with torch.no_grad():
                for (idx, batch) in enumerate(loaders[i]):
                    adj, features, edge_features, edges, labels, dist, triangles = batch
                    labels = labels.to(device)

                    if config['combined_model']:
                        features, edge_features = features.to(device), edge_features.to(device)
                        scores = combined_model(features, edge_features, edges, triangles)
                    else:
                        # model
                        if config["gnn_type"] == "dgnn":
                            dist = dist.float()
                            features, dist = features.to(device), dist.to(device)
                            out = model(features, dist)

                        if config["gnn_type"] == "egnnc":
                            features, edge_features = features.to(device), edge_features.to(device)
                            out = model(features, edge_features)


                        # classify
                        if config["classifier"] == "mlp":
                            
                            out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = sigmoid(scores1 + scores2)

                        # classify
                        if config["classifier"] == "mlp_tri":
                            #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                            uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                            #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores11 = mlp(uvzw)
                            scores12 = mlp(uvwz)
                            scores21 = mlp(vuzw)
                            scores22 = mlp(vuwz)
                            scores1 = (scores11 + scores21)/2
                            scores2 = (scores21 + scores22)/2
                            scores = sigmoid(scores1 + scores2)


                        if config["classifier"] == "mlp_bi_ntn":
                            out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = 1-sigmoid(scores1 + scores2)

                        if config["classifier"] == "cnn":           
                            out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)#concat_node_representations_double_triangle
                    
                            scores = sigmoid(cnn(out1))
    

                        if config["classifier"] == "pos_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = all_pairs[edges.T]
                        
                        if config["classifier"] == "neg_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = 1 - all_pairs[edges.T]

                    y_true.extend(labels.detach().cpu().numpy())
                    y_scores.extend(scores.detach().cpu().numpy())
                    if (idx + 1) % stats_per_batch == 0:
                        print('    Batch {} / {}, Graph {} / {}'.format(idx+1, num_batches, i+1, len(datasets)))
        y_true = np.array(y_true).flatten()
        y_scores = np.array(y_scores).flatten()
        area = roc_auc_score(y_true, y_scores)
        print('ROC-AUC score: {:.4f}'.format(area))
        print('--------------------------------')

    # Train.
    if not config['val'] and not config['test']:
        if config['combined_model']:
            optimizer = optim.Adam(combined_model.parameters(), lr=config['lr'],
                                weight_decay=config['weight_decay'])
        else:
            optimizer = optim.Adam(model.parameters(), lr=config['lr'],
                                weight_decay=config['weight_decay'])
        epochs = config['epochs']

        #scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=1500, gamma=0.8)
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[5, 10, 15, 20, 25], gamma=0.5) # Epoch decay
        if config['combined_model']:
            combined_model.train()
        else:
            model.train()
            if config["classifier"] == "mlp" or config["classifier"] == "mlp_tri" :
                mlp.train()
            if config["classifier"] == "cnn":
                cnn.train()
        print('--------------------------------')
        print('Training.')
        loss_vals = []
        for epoch in range(epochs):
            _epoch_loss = []
            print('Epoch {} / {}'.format(epoch+1, epochs))

            epoch_loss = 0.0
            epoch_roc = 0.0
            epoch_batches = 0
            shuffle = list(range(len(loaders)))
            random.shuffle(shuffle) # Shuffle order of graphs
            for i in shuffle:
                num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
                epoch_batches += num_batches
                graph_roc = 0.0
                running_loss = 0.0
                for (idx, batch) in enumerate(loaders[i]):
                    adj, features, edge_features, edges, labels, dist, triangles = batch
                    labels = labels.to(device)
                    optimizer.zero_grad()
                    if config['combined_model']:
                        features, edge_features = features.to(device), edge_features.to(device)
                        scores = combined_model(features, edge_features, edges, triangles)
                    else:

                        # model
                        if config["gnn_type"] == "dgnn":
                            dist = dist.float()
                            features, dist = features.to(device), dist.to(device)
                            out = model(features, dist)

                        if config["gnn_type"] == "egnnc":
                            features, edge_features = features.to(device), edge_features.to(device)
                            out = model(features, edge_features)

                        # classify
                        if config["classifier"] == "mlp":
                            out1, out2 = utils.concat_node_representations_double(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = sigmoid(scores1 + scores2)

                        # classify
                        if config["classifier"] == "mlp_tri":
                            #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                            uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                            #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores11 = mlp(uvzw)
                            scores12 = mlp(uvwz)
                            scores21 = mlp(vuzw)
                            scores22 = mlp(vuwz)
                            scores1 = (scores11 + scores21)/2
                            scores2 = (scores21 + scores22)/2
                            scores = sigmoid(scores1 + scores2)

                        if config["classifier"] == "mlp_bi_ntn":
                            out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = 1-sigmoid(scores1 + scores2)
                        
                        if config["classifier"] == "cnn":           
                            out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)#concat_node_representations_double_triangle
                    
                            scores = sigmoid(cnn(out1))                

                        if config["classifier"] == "pos_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = all_pairs[edges.T]
                        
                        if config["classifier"] == "neg_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = 1 - all_pairs[edges.T]

                    loss = criterion(scores, labels.float()) # Loss function for BCE loss
                    #loss = utils.get_focal_loss_criterion(scores, labels.float())  # Loss function for Focal Loss 
                    loss.backward()
                    optimizer.step()
                    with torch.no_grad():
                        running_loss += loss.item()
                        epoch_loss += loss.item()
                        _epoch_loss.append(loss.item())
                        if (torch.sum(labels.long() == 0).item() > 0) and (torch.sum(labels.long() == 1).item() > 0):
                            area = roc_auc_score(labels.detach().cpu().numpy(), scores.detach().cpu().numpy())
                            epoch_roc += area
                            graph_roc += area
                running_loss /= num_batches
                print('    Graph {} / {}: loss {:.4f}'.format(
                    i+1, len(datasets), running_loss))
                print('    ROC-AUC score: {:.4f}'.format(graph_roc/num_batches))

            scheduler.step()
            print("Epoch avg loss: {}".format(epoch_loss / epoch_batches))
            print("Epoch avg ROC_AUC score: {}".format(epoch_roc / epoch_batches))
            loss_vals.append(sum(_epoch_loss)/len(_epoch_loss))

        plt.plot(np.linspace(1, epochs, epochs).astype(int), loss_vals)
        plt.xlabel('epochs')
        plt.title('Training Loss')
        plt.legend()
        
        
        plt.savefig('../results/training_loss.png')
        plt.show(block=False)
        plt.close()
         
         
        print('Finished training.')
        print('--------------------------------')

    # Save trained model
    if not config['val'] and not config['test']:
        if config['save']:
            print('--------------------------------')
            directory = config['saved_models_dir']
            # directory = os.path.join(os.path.dirname(os.getcwd()),
            #                         'trained_models')
            if not os.path.exists(directory):
                os.makedirs(directory)
            fname = utils.get_fname(config)
            path = os.path.join(directory, fname)
            print('Saving model at {}'.format(path))
            if config['combined_model']:
                torch.save(combined_model.state_dict(), path)
            else:
                
                torch.save(model.state_dict(), path)


                if config["classifier"] == "mlp":
                    fnamemlp = 'mlp' + fname
                    pathmlp = os.path.join(directory, fnamemlp)
                    torch.save(mlp.state_dict(), pathmlp)
                print('Finished saving model.')
                print('--------------------------------')

                if config["classifier"] == "mlp_tri":
                    fnamemlp = 'mlp_tri' + fname
                    pathmlp = os.path.join(directory, fnamemlp)
                    torch.save(mlp.state_dict(), pathmlp)
                print('Finished saving model.')
                print('--------------------------------')

                if config["classifier"] == "mlp_bi_ntn":
                    fnamemlp = 'mlp_bi_ntn' + fname
                    pathmlp = os.path.join(directory, fnamemlp)
                    torch.save(mlp.state_dict(), pathmlp)
                print('Finished saving model.')
                print('--------------------------------')
            
                if config["classifier"] == "cnn":
                    fnamemlp = 'cnn' + fname
                    pathmlp = os.path.join(directory, fnamemlp)
                    torch.save(cnn.state_dict(), pathmlp)
                print('Finished saving model.')
                print('--------------------------------')

    # Compute ROC-AUC score on validation set after training.
    if not config['val'] and not config['test']:
        print('--------------------------------')
        print('Computing ROC-AUC score for the validation dataset after training.')
        if not config['val']:
            dataset_args = ('val', config['num_layers'])
            
            datasets = utils.get_dataset_gcn(dataset_args, config['dataset_folder'], is_debug=config["is_debug"])
            
            loaders = []
            for i in range(len(datasets)):
                loaders.append(DataLoader(dataset=datasets[i], batch_size=config['batch_size'],
                                    shuffle=False, collate_fn=datasets[i].collate_wrapper))
        y_true, y_scores = [], []
        for i in range(len(datasets)):
            num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
            with torch.no_grad():
                for (idx, batch) in enumerate(loaders[i]):
                    adj, features, edge_features, edges, labels, dist, triangles = batch
                    labels = labels.to(device)
                    
                    if config["combined_model"]:    
                        features, edge_features = features.to(device), edge_features.to(device)
                        scores = combined_model(features, edge_features, edges, triangles)
                    else:
                        # model
                        if config["gnn_type"] == "dgnn":
                            dist = dist.float()
                            features, dist = features.to(device), dist.to(device)
                            out = model(features, dist)
    
                        if config["gnn_type"] == "egnnc":
                            features, edge_features = features.to(device), edge_features.to(device)
                            out = model(features, edge_features)


                        # classify
                        if config["classifier"] == "mlp":
                        
                            out1, out2 = utils.concat_node_representations_double(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = sigmoid(scores1 + scores2)

                        # classify
                        if config["classifier"] == "mlp_tri":
                            #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                            uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                            #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores11 = mlp(uvzw)
                            scores12 = mlp(uvwz)
                            scores21 = mlp(vuzw)
                            scores22 = mlp(vuwz)
                            scores1 = (scores11 + scores21)/2
                            scores2 = (scores21 + scores22)/2
                            scores = sigmoid(scores1 + scores2)

                        if config["classifier"] == "mlp_bi_ntn":
                            out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = 1-sigmoid(scores1 + scores2)
                        
                        if config["classifier"] == "cnn":           
                            out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)#concat_node_representations_double_triangle
                    
                            scores = sigmoid(cnn(out1))                       

                        if config["classifier"] == "pos_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = all_pairs[edges.T]
                        
                        if config["classifier"] == "neg_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = 1 - all_pairs[edges.T]

                    y_true.extend(labels.detach().cpu().numpy())
                    y_scores.extend(scores.detach().cpu().numpy())
             

                    if (idx + 1) % stats_per_batch == 0:
                        print('    Batch {} / {}, Graph {} / {}'.format(idx+1, num_batches, i+1, len(datasets)))
        y_true = np.array(y_true).flatten()
        y_scores = np.array(y_scores).flatten()
 
        area = roc_auc_score(y_true, y_scores)
        print('ROC-AUC score: {:.4f}'.format(area))
        print('--------------------------------')

    # Plot the true positive rate and true negative rate vs threshold.
    if not config['val'] and not config['test']:
        tpr, fpr, thresholds = roc_curve(y_true, y_scores)
        tnr = 1 - fpr
        plt.plot(thresholds, tpr, label='tpr')
        plt.plot(thresholds, tnr, label='tnr')
        plt.xlabel('Threshold')
        plt.title('TPR / TNR vs Threshold')
        plt.legend()
        plt.savefig('../results/tpr-tnr_v_threshold.png')
        plt.show(block=False)
        plt.close()

        # Choose an appropriate threshold and generate classification report on the validation set.
        idx1 = np.where(tpr <= tnr)[0]
        idx2 = np.where(tpr >= tnr)[0]
        t = config['threshold']#thresholds[idx1[-1]]
        _thres = config['threshold']
        for t in _thres:
            running_loss, total_loss = 0.0, 0.0
            num_correct, num_examples = 0, 0
            total_correct, total_examples, total_batches = 0, 0, 0
            y_true, y_scores, y_pred = [], [], []
            for i in range(len(datasets)):
                num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
                total_batches += num_batches
                # Added by Jorge
                edge_pred, neg_pred, classes, coords = [], [], None, None
                # --------------
                with torch.no_grad():
                    for (idx, batch) in enumerate(loaders[i]):
                        adj, features, edge_features, edges, labels, dist, triangles = batch
                        labels = labels.to(device)
                        
                        if config["combined_model"]:
                            features, edge_features = features.to(device), edge_features.to(device)
                            scores = combined_model(features, edge_features, edges, triangles)
                        else:
                            # model
                            if config["gnn_type"] == "dgnn":
                                dist = dist.float()
                                features, dist = features.to(device), dist.to(device)
                                out = model(features, dist)

                            if config["gnn_type"] == "egnnc":
                                features, edge_features = features.to(device), edge_features.to(device)
                                out = model(features, edge_features)


                            # classify
                            if config["classifier"] == "mlp":
                            
                                out1, out2 = utils.concat_node_representations_double(out, edges, device)
                                scores1 = mlp(out1)
                                scores2 = mlp(out2)
                                scores = sigmoid(scores1 + scores2)

                            # classify
                            if config["classifier"] == "mlp_tri":
                                #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                                uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                                #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                                scores11 = mlp(uvzw)
                                scores12 = mlp(uvwz)
                                scores21 = mlp(vuzw)
                                scores22 = mlp(vuwz)
                                scores1 = (scores11 + scores21)/2
                                scores2 = (scores21 + scores22)/2
                                scores = sigmoid(scores1 + scores2)

                            if config["classifier"] == "mlp_bi_ntn":
                                out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                                scores1 = mlp(out1)
                                scores2 = mlp(out2)
                                scores = 1-sigmoid(scores1 + scores2)

                            if config["classifier"] == "cnn":           
                                out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)#concat_node_representations_double_triangle
                        
                                scores = sigmoid(cnn(out1))                        

                            if config["classifier"] == "pos_sig":
                                all_pairs = torch.mm(out, out.t())
                                all_pairs = sigmoid(all_pairs)
                                scores = all_pairs[edges.T]
                            
                            if config["classifier"] == "neg_sig":
                                all_pairs = torch.mm(out, out.t())
                                all_pairs = sigmoid(all_pairs)
                                scores = 1 - all_pairs[edges.T]

                        loss = criterion(scores, labels.float()) # Loss function for BCE Loss 
                        #loss = utils.get_focal_loss_criterion(scores, labels.float())  # Loss function for Focal Loss 

                        running_loss += loss.item()
                        total_loss += loss.item()
                        predictions = (scores >= t).long()

                        # added by Jorge
                        preds = edges[torch.nonzero(predictions).detach().cpu().numpy(), :]
                        neg_preds = edges[(predictions == 0).detach().cpu().nonzero(), :]
                        if neg_preds.shape == (2,):
                            neg_preds = neg_preds.reshape(1, 1, -1)

                        if len(preds) > 0:
                            for pred in preds:
                                edge_pred.append([pred[0][0], pred[0][1]])
                        
                        if len(neg_preds) > 0:
                            for pred in neg_preds:
                                neg_pred.append([pred[0][0], pred[0][1]])
                        # ---------------

                        num_correct += torch.sum(predictions == labels.long()).item()
                        total_correct += torch.sum(predictions == labels.long()).item()
                        num_examples += len(labels)
                        total_examples += len(labels)
                        y_true.extend(labels.detach().cpu().numpy())
                        y_scores.extend(scores.detach().cpu().numpy())
                        y_pred.extend(predictions.detach().cpu().numpy())
                        if (idx + 1) % stats_per_batch == 0:
                            running_loss /= stats_per_batch
                            accuracy = num_correct / num_examples
                            print('    Batch {} / {}, Graph {} / {}: loss {:.4f}, accuracy {:.4f}'.format(
                                idx+1, num_batches, i+1, len(datasets), running_loss, accuracy))
                            if (torch.sum(labels.long() == 0).item() > 0) and (torch.sum(labels.long() == 1).item() > 0):
                                area = roc_auc_score(labels.detach().cpu().numpy(), scores.detach().cpu().numpy())
                                print('    ROC-AUC score: {:.4f}'.format(area))
                            running_loss = 0.0
                            num_correct, num_examples = 0, 0
                    running_loss = 0.0
                    num_correct, num_examples = 0, 0

                    # export as json for visualization in IntelliGraph JORGE
                    utils.export_prediction_as_json(datasets[i].path[0].replace("_forGraphSAGE_edges.csv", ""), 'train_val', edge_pred, neg_pred, config['results_dir'], config['model_id'])

            total_loss /= total_batches
            total_accuracy = total_correct / total_examples
            print('Loss {:.4f}, accuracy {:.4f}'.format(total_loss, total_accuracy))
            print('Threshold: {:.4f}, accuracy: {:.4f}'.format(t, total_correct / total_examples))
            y_true = np.array(y_true).flatten()
            y_scores = np.array(y_scores).flatten()
            y_pred = np.array(y_pred).flatten()
            report = classification_report(y_true, y_pred, digits=4)
            area = roc_auc_score(y_true, y_scores)
            print('ROC-AUC score: {:.4f}'.format(area))
            print("Threshold: ", t)
            print('Classification report\n', report)
        print('Finished validating.')
        print('--------------------------------')

    # =================== Validation ===================
    if config['val']:
        criterion = utils.get_criterion(config['task'])
        stats_per_batch = config['stats_per_batch']

        t = config['threshold']
        if config["combined_model"]:
            combined_model.eval()
        else:
            model.eval()
            if config["classifier"] == "mlp" or config["classifier"] == "mlp_tri":
                mlp.eval()
            if config["classifier"] == "cnn":
                cnn.eval()
        print('--------------------------------')
        print('Computing ROC-AUC score for the validation dataset after training.')
        _thres = config['threshold']
        for t in _thres:
            
            running_loss, total_loss = 0.0, 0.0
            num_correct, num_examples = 0, 0
            total_correct, total_examples, total_batches = 0, 0, 0
            y_true, y_scores, y_pred = [], [], []

            for i in range(len(datasets)):
                num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
                total_batches += num_batches
                # Added by Jorge
                edge_pred, neg_pred, classes, coords = [], [], None, None
                # --------------
                for (idx, batch) in enumerate(loaders[i]):
                    adj, features, edge_features, edges, labels, dist, triangles = batch
                    labels = labels.to(device)
                    if config["combined_model"]:
                        features, edge_features = features.to(device), edge_features.to(device)
                        scores = combined_model(features, edge_features, edges, triangles)
                    else:
                        # model
                        if config["gnn_type"] == "dgnn":
                            dist = dist.float()
                            features, dist = features.to(device), dist.to(device)
                            out = model(features, dist)

                        if config["gnn_type"] == "egnnc":
                                features, edge_features = features.to(device), edge_features.to(device)
                                out = model(features, edge_features)


                        # classify
                        if config["classifier"] == "mlp":
                            
                            out1, out2 = utils.concat_node_representations_double(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = sigmoid(scores1 + scores2)

                            # classify
                        if config["classifier"] == "mlp_tri":
                            #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                            uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                            #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores11 = mlp(uvzw)
                            scores12 = mlp(uvwz)
                            scores21 = mlp(vuzw)
                            scores22 = mlp(vuwz)
                            scores1 = (scores11 + scores21)/2
                            scores2 = (scores21 + scores22)/2
                            scores = sigmoid(scores1 + scores2)

                        if config["classifier"] == "mlp_bi_ntn":
                            out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = 1-sigmoid(scores1 + scores2)
                            
                        if config["classifier"] == "cnn":
                            out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)
                            scores = sigmoid(cnn(out1))
                            

                        if config["classifier"] == "pos_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = all_pairs[edges.T]
                        
                        if config["classifier"] == "neg_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = 1 - all_pairs[edges.T]

                    loss = criterion(scores, labels.float()) # Loss function for BCE Loss 
                    #loss = utils.get_focal_loss_criterion(scores, labels.float()) # Loss function for Focal Loss 

                    running_loss += loss.item()
                    total_loss += loss.item()
                    predictions = (scores >= t).long()
                    # added by Jorge
                    preds = edges[torch.nonzero(predictions).detach().cpu().numpy(), :]
                    neg_preds = edges[(predictions == 0).detach().cpu().nonzero(), :]
                    if neg_preds.shape == (2,):
                        neg_preds = neg_preds.reshape(1, 1, -1)
                    # print(neg_preds)
                    # print(type(neg_preds))
                    # print(neg_preds.shape)
                    if len(preds) > 0:
                        for pred in preds:
                            edge_pred.append([pred[0][0], pred[0][1]])
                    if len(neg_preds) > 0:
                        for pred in neg_preds:
                            neg_pred.append([pred[0][0], pred[0][1]])
                    # ---------------

                    num_correct += torch.sum(predictions == labels.long()).item()
                    total_correct += torch.sum(predictions == labels.long()).item()
                    num_examples += len(labels)
                    total_examples += len(labels)
                    y_true.extend(labels.detach().cpu().numpy())
                    y_scores.extend(scores.detach().cpu().numpy())
                    y_pred.extend(predictions.detach().cpu().numpy())
                    if (idx + 1) % stats_per_batch == 0:
                        running_loss /= stats_per_batch
                        accuracy = num_correct / num_examples
                        print('    Batch {} / {}: loss {:.4f}, accuracy {:.4f}'.format(
                            idx+1, num_batches, running_loss, accuracy))
                        if (torch.sum(labels.long() == 0).item() > 0) and (torch.sum(labels.long() == 1).item() > 0):
                            area = roc_auc_score(labels.detach().cpu().numpy(), scores.detach().cpu().numpy())
                            print('    ROC-AUC score: {:.4f}'.format(area))
                        running_loss = 0.0
                        num_correct, num_examples = 0, 0

                running_loss = 0.0
                num_correct, num_examples = 0, 0

                # export as json for visualization in IntelliGraph JORGE
                utils.export_prediction_as_json(datasets[i].path[0].replace("_forGraphSAGE_edges.csv", ""), 'validation', edge_pred, neg_pred, config['results_dir'], config['model_id'])

            total_loss /= total_batches
            total_accuracy = total_correct / total_examples
            print('Loss {:.4f}, accuracy {:.4f}'.format(total_loss, total_accuracy))
            y_true = np.array(y_true).flatten()
            y_scores = np.array(y_scores).flatten()
            y_pred = np.array(y_pred).flatten()
            report = classification_report(y_true, y_pred, digits=4)
            area = roc_auc_score(y_true, y_scores)
            print('ROC-AUC score: {:.4f}'.format(area))
            print("Threshold: ", t)
            print('Classification report\n', report)
            print('Finished Validation Set')
            print('--------------------------------')


    # =================== Test ===================
        # Evaluate on test set.
    if config['test']:
        criterion = utils.get_criterion(config['task'])
        stats_per_batch = config['stats_per_batch']

        #t = config['threshold']
        _thres = config['threshold']
        for t in _thres:
            if config["combined_model"]:    
                combined_model.eval()
            else:
                model.eval()
                if config["classifier"] == "mlp" or config["classifier"] == "mlp_tri":
                    mlp.eval()
                if config["classifier"] == "cnn":
                    cnn.eval()
            print('--------------------------------')
            print('Computing ROC-AUC score for the test dataset after training.')
            running_loss, total_loss = 0.0, 0.0
            num_correct, num_examples = 0, 0
            total_correct, total_examples, total_batches = 0, 0, 0
            y_true, y_scores, y_pred = [], [], []
            for i in range(len(datasets)):
                num_batches = int(ceil(len(datasets[i]) / config['batch_size']))
                total_batches += num_batches
                # Added by Jorge
                edge_pred, neg_pred = [], []
                # --------------

                for (idx, batch) in enumerate(loaders[i]):
                    adj, features, edge_features, edges, labels, dist, triangles = batch
                    labels = labels.to(device)
                    
                    if config["combined_model"]:
                        features, edge_features = features.to(device), edge_features.to(device)
                        scores = combined_model(features, edge_features, edges, triangles)
                    else:
                        # model
                        if config["gnn_type"] == "dgnn":
                            dist = dist.float()
                            features, dist = features.to(device), dist.to(device)
                            out = model(features, dist)
                        
                        if config["gnn_type"] == "egnnc":
                                features, edge_features = features.to(device), edge_features.to(device)
                                out = model(features, edge_features)


                        # classify
                        if config["classifier"] == "mlp":
                        
                            out1, out2 = utils.concat_node_representations_double(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = sigmoid(scores1 + scores2)

                            # classify
                        if config["classifier"] == "mlp_tri":
                            #uvz, vuz, vzu, uzv, zvu, zuv, uvw, vuw, vwu, uwv, wuv, wvu 
                            uvzw, uvwz, vuzw, vuwz = utils.concat_node_representations_double_triangle(out, edges, triangles, device)
                            #out1, out2 = utils.concat_node_representations_double(out, edges, device)#concat_node_representations_double_triangle
                            scores11 = mlp(uvzw)
                            scores12 = mlp(uvwz)
                            scores21 = mlp(vuzw)
                            scores22 = mlp(vuwz)#
                            scores1 = (scores11 + scores21)/2
                            scores2 = (scores21 + scores22)/2
                            scores = sigmoid(scores1 + scores2)

                        if config["classifier"] == "mlp_bi_ntn":
                            out1, out2 = utils.concat_node_respresentations_double_with_biNTN(out, edges, device)
                            scores1 = mlp(out1)
                            scores2 = mlp(out2)
                            scores = 1-sigmoid(scores1 + scores2)
                            
                        if config["classifier"] == "cnn":
                            out1 = utils.create_TriangularMotifsCNN_input(out, edges, triangles, device)
                            scores = sigmoid(cnn(out1))                    

                        if config["classifier"] == "pos_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = all_pairs[edges.T]
                        
                        if config["classifier"] == "neg_sig":
                            all_pairs = torch.mm(out, out.t())
                            all_pairs = sigmoid(all_pairs)
                            scores = 1 - all_pairs[edges.T]
                        
                    loss = criterion(scores, labels.float())  # Loss function for BCE Loss 
                   
                    #loss = utils.get_focal_loss_criterion(scores, labels.float())  # Loss function for Focal Loss 
                    running_loss += loss.item()
                    total_loss += loss.item()
                    predictions = (scores >= t).long()

                    # added by Jorge
                    preds = edges[torch.nonzero(predictions).detach().cpu().numpy(), :]
                    neg_preds = edges[(predictions == 0).detach().cpu().nonzero(), :]
                    if neg_preds.shape == (2,):
                        neg_preds = neg_preds.reshape(1, 1, -1)

                    if len(preds) > 0:
                        for pred in preds:
                            edge_pred.append([pred[0][0], pred[0][1]])

                    # ---------------

                    num_correct += torch.sum(predictions == labels.long()).item()
                    total_correct += torch.sum(predictions == labels.long()).item()
                    num_examples += len(labels)
                    total_examples += len(labels)
                    y_true.extend(labels.detach().cpu().numpy())
                    y_scores.extend(scores.detach().cpu().numpy())
                    y_pred.extend(predictions.detach().cpu().numpy())
                    if (idx + 1) % stats_per_batch == 0:
                        running_loss /= stats_per_batch
                        accuracy = num_correct / num_examples
                        print('    Batch {} / {}: loss {:.4f}, accuracy {:.4f}'.format(
                            idx+1, num_batches, running_loss, accuracy))
                        if (torch.sum(labels.long() == 0).item() > 0) and (torch.sum(labels.long() == 1).item() > 0):
                            area = roc_auc_score(labels.detach().cpu().numpy(), scores.detach().cpu().numpy())
                            print('    ROC-AUC score: {:.4f}'.format(area))
                        running_loss = 0.0
                        num_correct, num_examples = 0, 0

                running_loss = 0.0
                num_correct, num_examples = 0, 0

                # export as json for visualization in IntelliGraph JORGE
                utils.export_prediction_as_json(datasets[i].path[0].replace("_forGraphSAGE_edges.csv", ""), 'testing', edge_pred, neg_pred, config['results_dir'], config['model_id'])


            total_loss /= total_batches
            total_accuracy = total_correct / total_examples
            print('Loss {:.4f}, accuracy {:.4f}'.format(total_loss, total_accuracy))
            y_true = np.array(y_true).flatten()
            y_scores = np.array(y_scores).flatten()
            y_pred = np.array(y_pred).flatten()
            report = classification_report(y_true, y_pred, digits=4)
            area = roc_auc_score(y_true, y_scores)
            print('ROC-AUC score: {:.4f}'.format(area))
            print("Threshold: ", t)
            print('Classification report\n', report)
        print('Finished testing.')
        print('--------------------------------')

if __name__ == '__main__':
    main()
