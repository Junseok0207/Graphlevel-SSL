import os
import torch
import pickle
import collections
import math
import pandas as pd
import numpy as np
import networkx as nx
import random
from rdkit import Chem
from rdkit.Chem import Descriptors
from rdkit.Chem import AllChem
from rdkit import DataStructs
from rdkit.Chem.rdMolDescriptors import GetMorganFingerprintAsBitVect
from torch.utils import data
from torch_geometric.data import Data
from torch_geometric.data import InMemoryDataset, Dataset
from torch_geometric.data import Batch
from itertools import repeat, product, chain
from torch_geometric.utils import dropout_adj, degree, to_undirected, to_networkx

num_atom_type = 120 #including the extra mask tokens=119
num_chirality_tag = 3 # original =3. including the extra mask tokens=3
num_bond_type = 6 #including aromatic and self-loop edge, and extra masked tokens
num_bond_direction = 3 # original =3, inlcuding the extra mask tokens=3

# allowable node and edge features
allowable_features = {
    'possible_atomic_num_list' : list(range(1, 119)),
    'possible_formal_charge_list' : [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5],
    'possible_chirality_list' : [
        Chem.rdchem.ChiralType.CHI_UNSPECIFIED,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CCW,
        Chem.rdchem.ChiralType.CHI_OTHER
    ],
    'possible_hybridization_list' : [
        Chem.rdchem.HybridizationType.S,
        Chem.rdchem.HybridizationType.SP, Chem.rdchem.HybridizationType.SP2,
        Chem.rdchem.HybridizationType.SP3, Chem.rdchem.HybridizationType.SP3D,
        Chem.rdchem.HybridizationType.SP3D2, Chem.rdchem.HybridizationType.UNSPECIFIED
    ],
    'possible_numH_list' : [0, 1, 2, 3, 4, 5, 6, 7, 8],
    'possible_implicit_valence_list' : [0, 1, 2, 3, 4, 5, 6],
    'possible_degree_list' : [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    'possible_bonds' : [
        Chem.rdchem.BondType.SINGLE,
        Chem.rdchem.BondType.DOUBLE,
        Chem.rdchem.BondType.TRIPLE,
        Chem.rdchem.BondType.AROMATIC
    ],
    'possible_bond_dirs' : [ # only for double bond stereo information
        Chem.rdchem.BondDir.NONE,
        Chem.rdchem.BondDir.ENDUPRIGHT,
        Chem.rdchem.BondDir.ENDDOWNRIGHT
    ]
}

def mol_to_graph_data_obj_simple(mol):
    """
    Converts rdkit mol object to graph Data object required by the pytorch
    geometric package. NB: Uses simplified atom and bond features, and represent
    as indices
    :param mol: rdkit mol object
    :return: graph data object with the attributes: x, edge_index, edge_attr
    """
    # atoms
    num_atom_features = 2   # atom type,  chirality tag
    atom_features_list = []
    for atom in mol.GetAtoms():
        atom_feature = [allowable_features['possible_atomic_num_list'].index(
            atom.GetAtomicNum())] + [allowable_features[
            'possible_chirality_list'].index(atom.GetChiralTag())]
        atom_features_list.append(atom_feature)
    x = torch.tensor(np.array(atom_features_list), dtype=torch.long)

    # bonds
    num_bond_features = 2   # bond type, bond direction
    if len(mol.GetBonds()) > 0: # mol has bonds
        edges_list = []
        edge_features_list = []
        for bond in mol.GetBonds():
            i = bond.GetBeginAtomIdx()
            j = bond.GetEndAtomIdx()
            edge_feature = [allowable_features['possible_bonds'].index(
                bond.GetBondType())] + [allowable_features[
                                            'possible_bond_dirs'].index(
                bond.GetBondDir())]
            edges_list.append((i, j))
            edge_features_list.append(edge_feature)
            edges_list.append((j, i))
            edge_features_list.append(edge_feature)

        # data.edge_index: Graph connectivity in COO format with shape [2, num_edges]
        edge_index = torch.tensor(np.array(edges_list).T, dtype=torch.long)

        # data.edge_attr: Edge feature matrix with shape [num_edges, num_edge_features]
        edge_attr = torch.tensor(np.array(edge_features_list),
                                 dtype=torch.long)
    else:   # mol has no bonds
        edge_index = torch.empty((2, 0), dtype=torch.long)
        edge_attr = torch.empty((0, num_bond_features), dtype=torch.long)

    data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)

    return data

def graph_data_obj_to_mol_simple(data_x, data_edge_index, data_edge_attr):
    """
    Convert pytorch geometric data obj to rdkit mol object. NB: Uses simplified
    atom and bond features, and represent as indices.
    :param: data_x:
    :param: data_edge_index:
    :param: data_edge_attr
    :return:
    """
    mol = Chem.RWMol()

    # atoms
    atom_features = data_x.cpu().numpy()
    num_atoms = atom_features.shape[0]
    for i in range(num_atoms):
        atomic_num_idx, chirality_tag_idx = atom_features[i]
        atomic_num = allowable_features['possible_atomic_num_list'][atomic_num_idx]
        chirality_tag = allowable_features['possible_chirality_list'][chirality_tag_idx]
        atom = Chem.Atom(atomic_num)
        atom.SetChiralTag(chirality_tag)
        mol.AddAtom(atom)

    # bonds
    edge_index = data_edge_index.cpu().numpy()
    edge_attr = data_edge_attr.cpu().numpy()
    num_bonds = edge_index.shape[1]
    for j in range(0, num_bonds, 2):
        begin_idx = int(edge_index[0, j])
        end_idx = int(edge_index[1, j])
        bond_type_idx, bond_dir_idx = edge_attr[j]
        bond_type = allowable_features['possible_bonds'][bond_type_idx]
        bond_dir = allowable_features['possible_bond_dirs'][bond_dir_idx]
        mol.AddBond(begin_idx, end_idx, bond_type)
        # set bond direction
        new_bond = mol.GetBondBetweenAtoms(begin_idx, end_idx)
        new_bond.SetBondDir(bond_dir)

    # Chem.SanitizeMol(mol) # fails for COC1=CC2=C(NC(=N2)[S@@](=O)CC2=NC=C(
    # C)C(OC)=C2C)C=C1, when aromatic bond is possible
    # when we do not have aromatic bonds
    # Chem.SanitizeMol(mol, sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)

    return mol

def graph_data_obj_to_nx_simple(data):
    """
    Converts graph Data object required by the pytorch geometric package to
    network x data object. NB: Uses simplified atom and bond features,
    and represent as indices. NB: possible issues with recapitulating relative
    stereochemistry since the edges in the nx object are unordered.
    :param data: pytorch geometric Data object
    :return: network x object
    """
    G = nx.Graph()

    # atoms
    atom_features = data.x.cpu().numpy()
    num_atoms = atom_features.shape[0]
    for i in range(num_atoms):
        atomic_num_idx, chirality_tag_idx = atom_features[i]
        G.add_node(i, atom_num_idx=atomic_num_idx, chirality_tag_idx=chirality_tag_idx)
        pass

    # bonds
    edge_index = data.edge_index.cpu().numpy()
    edge_attr = data.edge_attr.cpu().numpy()
    num_bonds = edge_index.shape[1]
    for j in range(0, num_bonds, 2):
        begin_idx = int(edge_index[0, j])
        end_idx = int(edge_index[1, j])
        bond_type_idx, bond_dir_idx = edge_attr[j]
        if not G.has_edge(begin_idx, end_idx):
            G.add_edge(begin_idx, end_idx, bond_type_idx=bond_type_idx,
                       bond_dir_idx=bond_dir_idx)

    return G

def nx_to_graph_data_obj_simple(G):
    """
    Converts nx graph to pytorch geometric Data object. Assume node indices
    are numbered from 0 to num_nodes - 1. NB: Uses simplified atom and bond
    features, and represent as indices. NB: possible issues with
    recapitulating relative stereochemistry since the edges in the nx
    object are unordered.
    :param G: nx graph obj
    :return: pytorch geometric Data object
    """
    # atoms
    num_atom_features = 2  # atom type,  chirality tag
    atom_features_list = []
    for _, node in G.nodes(data=True):
        atom_feature = [node['atom_num_idx'], node['chirality_tag_idx']]
        atom_features_list.append(atom_feature)
    x = torch.tensor(np.array(atom_features_list), dtype=torch.long)

    # bonds
    num_bond_features = 2  # bond type, bond direction
    if len(G.edges()) > 0:  # mol has bonds
        edges_list = []
        edge_features_list = []
        for i, j, edge in G.edges(data=True):
            edge_feature = [edge['bond_type_idx'], edge['bond_dir_idx']]
            edges_list.append((i, j))
            edge_features_list.append(edge_feature)
            edges_list.append((j, i))
            edge_features_list.append(edge_feature)

        # data.edge_index: Graph connectivity in COO format with shape [2, num_edges]
        edge_index = torch.tensor(np.array(edges_list).T, dtype=torch.long)

        # data.edge_attr: Edge feature matrix with shape [num_edges, num_edge_features]
        edge_attr = torch.tensor(np.array(edge_features_list),
                                 dtype=torch.long)
    else:   # mol has no bonds
        edge_index = torch.empty((2, 0), dtype=torch.long)
        edge_attr = torch.empty((0, num_bond_features), dtype=torch.long)

    data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)

    return data

def get_gasteiger_partial_charges(mol, n_iter=12):
    """
    Calculates list of gasteiger partial charges for each atom in mol object.
    :param mol: rdkit mol object
    :param n_iter: number of iterations. Default 12
    :return: list of computed partial charges for each atom.
    """
    Chem.rdPartialCharges.ComputeGasteigerCharges(mol, nIter=n_iter,
                                                  throwOnParamFailure=True)
    partial_charges = [float(a.GetProp('_GasteigerCharge')) for a in
                       mol.GetAtoms()]
    return partial_charges

def create_standardized_mol_id(smiles):
    """

    :param smiles:
    :return: inchi
    """
    if check_smiles_validity(smiles):
        # remove stereochemistry
        smiles = AllChem.MolToSmiles(AllChem.MolFromSmiles(smiles),
                                     isomericSmiles=False)
        mol = AllChem.MolFromSmiles(smiles)
        if mol != None: # to catch weird issue with O=C1O[al]2oc(=O)c3ccc(cn3)c3ccccc3c3cccc(c3)c3ccccc3c3cc(C(F)(F)F)c(cc3o2)-c2ccccc2-c2cccc(c2)-c2ccccc2-c2cccnc21
            if '.' in smiles: # if multiple species, pick largest molecule
                mol_species_list = split_rdkit_mol_obj(mol)
                largest_mol = get_largest_mol(mol_species_list)
                inchi = AllChem.MolToInchi(largest_mol)
            else:
                inchi = AllChem.MolToInchi(mol)
            return inchi
        else:
            return
    else:
        return


def drop_nodes(data, aug_ratio):

    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    drop_num = int(node_num  * aug_ratio)

    idx_drop = np.random.choice(node_num, drop_num, replace=False)
    idx_nondrop = [n for n in range(node_num) if not n in idx_drop]
    #print('idx_drop: ', idx_drop)
    #print('idx_nondrop sorted: ', idx_nondrop)
    idx_dict = {idx_nondrop[n]:n for n in list(range(len(idx_nondrop)))}
    #print(idx_dict)

    edge_index = data.edge_index.numpy()
    idx_drop = set(idx_drop)
    idx_nondrop_edge = []
    for i in range(edge_index.shape[1]):
        tmp = set(edge_index[:, i])
        if not idx_drop.intersection(tmp):
            idx_nondrop_edge.append(i)
            edge_index[0, i] = idx_dict[edge_index[0, i]]
            edge_index[1, i] = idx_dict[edge_index[1, i]]

    edge_index = torch.from_numpy(edge_index[:, idx_nondrop_edge])
    edge_attr = data.edge_attr[idx_nondrop_edge, :]

    try:
        data.x = data.x[idx_nondrop]
        data.edge_index = edge_index
        data.edge_attr = edge_attr
    except:
        data = data

    # print('data.x new', data.x)
    # print('data.x try index')
    # for i in range(data.x.size(0)):
    #     print(i, data.x[i])
    # print('edge_index new', data.edge_index)
    # print('edge_attr new', data.edge_attr)
    return data



def drop_nodes_random(data, aug_ratio):

    # print('data.x:', data.x)
    # print('data.edge_index:', data.edge_index)
    # print('data.edge_attr:', data.edge_attr)
    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    drop_num = int(node_num  * aug_ratio)

    idx_drop = np.random.choice(node_num, drop_num, replace=False)
    idx_nondrop = [n for n in range(node_num) if not n in idx_drop]
    # print('idx_drop,', idx_drop)
    # print('idx_nondrop', idx_nondrop)

    # drop node features
    ## data.x = data.x[idx_nondrop]

    # modify edge index and feature
    edge_index = data.edge_index.numpy()
    idx_drop = set(idx_drop)
    idx_nondrop_edge = []
    for i in range(edge_index.shape[1]):
        tmp = set(edge_index[:, i])
        if not idx_drop.intersection(tmp):
            idx_nondrop_edge.append(i)

    edge_index = torch.from_numpy(edge_index[:, idx_nondrop_edge])
    edge_attr = data.edge_attr[idx_nondrop_edge, :]

    data.edge_index = edge_index
    data.edge_attr = edge_attr
    if data.edge_index.shape[1] != data.edge_attr.shape[0]:
        print('data dropping failed!')
        return 
    # print(data.x)
    # print(data.edge_index)
    # print(data.edge_attr)

    return data


def drop_nodes_nonC(data, aug_ratio):

    # print('data.x:', data.x)
    # print('data.edge_index:', data.edge_index)
    # print('data.edge_attr:', data.edge_attr)
    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    drop_num = int(node_num  * aug_ratio)

    atom_number = data.x[:, 0].numpy()
    c_idx = np.where(atom_number == 5)[0]
    nonc_idx = np.where(atom_number != 5)[0]

    # get drop_idx
    if drop_num <= len(nonc_idx):
        idx_drop = np.random.choice(nonc_idx, drop_num, replace=False)
    else:
        tmp = np.random.choice(c_idx, drop_num-len(nonc_idx), replace=False)
        idx_drop = np.concatenate([nonc_idx, tmp], axis=0)
    
    idx_nondrop = [n for n in range(node_num) if not n in idx_drop]

    # print('idx_drop,', idx_drop)
    # print('idx_nondrop', idx_nondrop)

    # drop node features
    ## data.x = data.x[idx_nondrop]

    # modify edge index and feature
    edge_index = data.edge_index.numpy()
    idx_drop = set(idx_drop)
    idx_nondrop_edge = []
    for i in range(edge_index.shape[1]):
        tmp = set(edge_index[:, i])
        if not idx_drop.intersection(tmp):
            idx_nondrop_edge.append(i)

    edge_index = torch.from_numpy(edge_index[:, idx_nondrop_edge])
    edge_attr = data.edge_attr[idx_nondrop_edge, :]

    data.edge_index = edge_index
    data.edge_attr = edge_attr
    if data.edge_index.shape[1] != data.edge_attr.shape[0]:
        print('data dropping failed!')
        return 
    # print(data.x)
    # print(data.edge_index)
    # print(data.edge_attr)

    return data

def drop_nodes_C(data, aug_ratio):

    # print('data.x:', data.x)
    # print('data.edge_index:', data.edge_index)
    # print('data.edge_attr:', data.edge_attr)
    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    drop_num = int(node_num  * aug_ratio)

    atom_number = data.x[:, 0].numpy()
    c_idx = np.where(atom_number == 5)[0]

    # get drop_idx
    idx_drop = np.random.choice(c_idx, drop_num, replace=False)
    
    idx_nondrop = [n for n in range(node_num) if not n in idx_drop]

    # print('idx_drop,', idx_drop)
    # print('idx_nondrop', idx_nondrop)

    # drop node features
    ## data.x = data.x[idx_nondrop]

    # modify edge index and feature
    edge_index = data.edge_index.numpy()
    idx_drop = set(idx_drop)
    idx_nondrop_edge = []
    for i in range(edge_index.shape[1]):
        tmp = set(edge_index[:, i])
        if not idx_drop.intersection(tmp):
            idx_nondrop_edge.append(i)

    edge_index = torch.from_numpy(edge_index[:, idx_nondrop_edge])
    edge_attr = data.edge_attr[idx_nondrop_edge, :]

    data.edge_index = edge_index
    data.edge_attr = edge_attr
    if data.edge_index.shape[1] != data.edge_attr.shape[0]:
        print('data dropping failed!')
        return 
    # print(data.x)
    # print(data.edge_index)
    # print(data.edge_attr)

    return data


def add_edges(data, aug_ratio):

    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    permute_num = int(edge_num * aug_ratio)

    edge_index = data.edge_index.transpose(0, 1).numpy()
    edge_index_add = np.random.choice(node_num, (permute_num, 2))

    idx_drop = np.random.choice(edge_num, permute_num, replace=False)
    edge_index[idx_drop] = edge_index_add

    data.edge_index = torch.tensor(edge_index).transpose_(0, 1)
    return data


def permute_edges(data, aug_ratio):

    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    permute_num = int(edge_num * aug_ratio)

    edge_index = data.edge_index.transpose(0, 1).numpy()
    edge_index_add = np.random.choice(node_num, (permute_num, 2))

    idx_drop = np.random.choice(edge_num, permute_num, replace=False)
    edge_index[idx_drop] = edge_index_add

    data.edge_index = torch.tensor(edge_index).transpose_(0, 1)
    return data


def drop_edges(data, aug_ratio):

    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    permute_num = int(edge_num * aug_ratio)

    edge_index = data.edge_index.transpose(0, 1).numpy()

    idx_nondrop = np.random.choice(edge_num, edge_num-permute_num, replace=False)
    edge_index = edge_index[idx_nondrop, :]

    data.edge_index = torch.tensor(edge_index).transpose_(0, 1)
    data.edge_attr = data.edge_attr[idx_nondrop, :]
    return data


def subgraph(data, aug_ratio):

    node_num, _ = data.x.size()
    _, edge_num = data.edge_index.size()
    sub_num = int(node_num * aug_ratio)

    edge_index = data.edge_index.numpy()

    idx_sub = [np.random.randint(node_num, size=1)[0]]
    #print('idx_sub: ', idx_sub)
    idx_neigh = set([n for n in edge_index[1][edge_index[0]==idx_sub[0]]])
    #print('idx_neigh: ', idx_neigh)

    count = 0
    while len(idx_sub) <= sub_num:
        count = count + 1
        if count > node_num:
            break
        if len(idx_neigh) == 0:
            break
        sample_node = np.random.choice(list(idx_neigh))
        if sample_node in idx_sub:
            continue
        idx_sub.append(sample_node)
        idx_neigh.union(set([n for n in edge_index[1][edge_index[0]==idx_sub[-1]]]))
    # print('idx_sub_final: ', idx_sub)
    # print('idx_neigh_final: ', idx_neigh)

    idx_drop = [n for n in range(node_num) if not n in idx_sub]
    #print('idx_drop: ', idx_drop)
    idx_nondrop = sorted(idx_sub)
    #print('idx_nondrop sorted: ', idx_nondrop)
    idx_dict = {idx_nondrop[n]:n for n in list(range(len(idx_nondrop)))}
    #print(idx_dict)
    edge_index = data.edge_index.numpy()
    idx_drop = set(idx_drop)
    idx_nondrop_edge = []
    for i in range(edge_index.shape[1]):
        tmp = set(edge_index[:, i])
        if not idx_drop.intersection(tmp):
            idx_nondrop_edge.append(i)
            edge_index[0, i] = idx_dict[edge_index[0, i]]
            edge_index[1, i] = idx_dict[edge_index[1, i]]

    edge_index = torch.from_numpy(edge_index[:, idx_nondrop_edge])
    edge_attr = data.edge_attr[idx_nondrop_edge, :]

    try:
        data.x = data.x[idx_nondrop]
        data.edge_index = edge_index
        data.edge_attr = edge_attr
    except:
        data = data
    # print('data.x new', data.x)
    # print('data.x try index')
    # for i in range(data.x.size(0)):
    #     print(i, data.x[i])
    # print('edge_index new', data.edge_index)
    # print('edge_attr new', data.edge_attr)
    return data


def mask_attributes(data, aug_ratio, mask_edge = False):

    node_num, feat_dim = data.x.size()
    mask_num = int(node_num * aug_ratio)

    idx_mask = np.random.choice(node_num, mask_num, replace=False)
    data.x[idx_mask] = torch.tensor([num_atom_type-1, 0])

    if mask_edge:
        edge_index = data.edge_index.numpy()
        idx_mask = set(idx_mask)
        idx_mask_edge = []
        for i in range(edge_index.shape[1]):
            tmp = set(edge_index[:, i])
            if idx_mask.intersection(tmp):
                idx_mask_edge.append(i)
        data.edge_attr[idx_mask_edge] = torch.tensor([num_bond_type-1, 0])

    return data


def domain_aug(data, smiles_list, rule_indicator, rules, aug_times=1):
    row_idx = data.id.cpu().numpy()[0]
    #print(row_idx)
    s = smiles_list[row_idx]
    mol_obj = Chem.MolFromSmiles(s)
    
    mol_prev = mol_obj
    mol_next = None
    for time in range(aug_times):
        #print('aug time: ', time)
        non_zero_idx = list(np.where(rule_indicator[row_idx, :]!=0)[0])
        cnt = -1
        while len(non_zero_idx)!=0:
            col_idx = random.choice(non_zero_idx)

            # calculate counts
            rule = rules[col_idx]
            rxn = AllChem.ReactionFromSmarts(rule['smarts'])
            products = rxn.RunReactants((mol_prev,))

            cnt = len(products)
            if cnt != 0:
                break
            else:
                non_zero_idx.remove(col_idx)
        
        if cnt >= 1:
            aug_idx = random.choice(range(cnt))
            mol = products[aug_idx][0]
            try:
                Chem.SanitizeMol(mol)
            except: # TODO: add detailed exception
                pass

            mol_next = mol
            mol_prev = mol
            #rule_indicator[row_idx, col_idx] -= 1
        else:
            mol_next = mol_prev
    assert mol_next
    data = mol_to_graph_data_obj_simple(mol_next)
    return data


# def domain_aug_new(data, smiles_list, rule_indicator, rules, aug_times=1):
#     row_idx = data.id.cpu().numpy()[0]
#     s = smiles_list[row_idx]
#     mol_obj = Chem.MolFromSmiles(s)
    
#     mol_prev = mol_obj
#     mol_next = None
#     for time in range(aug_times):
#         ri = np.random.randint(2)
#         if ri==0:

#         non_zero_idx = list(np.where(rule_indicator[row_idx, :]!=0)[0])
#         cnt = -1
#         while len(non_zero_idx)!=0:
#             col_idx = random.choice(non_zero_idx)

#             # calculate counts
#             rule = rules[col_idx]
#             rxn = AllChem.ReactionFromSmarts(rule['smarts'])
#             products = rxn.RunReactants((mol_prev,))

#             cnt = len(products)
#             if cnt != 0:
#                 break
#             else:
#                 non_zero_idx.remove(col_idx)
        
#         if cnt >= 1:
#             aug_idx = random.choice(range(cnt))
#             mol = products[aug_idx][0]
#             try:
#                 Chem.SanitizeMol(mol)
#             except: # TODO: add detailed exception
#                 pass

#             mol_next = mol
#             mol_prev = mol
#         else:
#             mol_next = mol_prev
#     assert mol_next
#     data = mol_to_graph_data_obj_simple(mol_next)
#     return data


import json
import pickle as pkl
class MoleculeDataset(InMemoryDataset):
    def __init__(self,
                 root,
                 #data = None,
                 #slices = None,
                 transform=None,
                 pre_transform=None,
                 pre_filter=None,
                 dataset='esol',
                 aug='none', aug_ratio=None,
                 empty=False):
        """
        Adapted from qm9.py. Disabled the download functionality
        :param root: directory of the dataset, containing a raw and processed
        dir. The raw dir should contain the file containing the smiles, and the
        processed dir can either empty or a previously processed file
        :param dataset: name of the dataset. Currently only implemented for
        zinc250k, chembl_with_labels, tox21, hiv, bace, bbbp, clintox, esol,
        freesolv, lipophilicity, muv, pcba, sider, toxcast
        :param empty: if True, then will not load any data obj. For
        initializing empty dataset
        """
        self.dataset = dataset
        self.root = root
        self.aug = aug
        self.aug_ratio = aug_ratio
        self.get_count = 0

        super(MoleculeDataset, self).__init__(root, transform, pre_transform,
                                                 pre_filter)
        self.transform, self.pre_transform, self.pre_filter = transform, pre_transform, pre_filter

        if not empty:
            self.data, self.slices = torch.load(self.processed_paths[0])
            self.processed_smiles = pd.read_csv(os.path.join(self.processed_dir, 'smiles.csv'), header=None)
            assert len(self.processed_smiles) == len(self)
            if self.dataset == 'bbbp':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset.upper() +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'bace':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['mol'].tolist()
            elif self.dataset == 'clintox':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'sider':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'tox21':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'toxcast':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'_data.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'muv':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'hiv':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset.upper() +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            elif self.dataset == 'mutag':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'_188_data.can', sep=' ', header=None)
                self.original_smiles = raw_file[0].tolist()
            elif self.dataset == 'dti':
                raw_file = pd.read_csv(self.root + '/raw/' + self.dataset +'.csv', sep=',')
                self.original_smiles = raw_file['smiles'].tolist()
            else:
                print('original smiles list not found!')
            print('dataset smiles {:d} raw smiles {:d}'.format(len(self.processed_smiles), len(self.original_smiles)))

            
    def get(self, idx):
        every = 4000000
        self.get_count += 1
        if self.get_count % every == 0:
            print('my aug: {0}'.format(self.aug))
        data = Data()
        for key in self.data.keys:
            item, slices = self.data[key], self.slices[key]
            s = list(repeat(slice(None), item.dim()))
#            print(key, item)
            s[data.__cat_dim__(key, item)] = slice(slices[idx],
                                                    slices[idx + 1])
            data[key] = item[s]

        if self.aug == 'dropN_random':
            data = drop_nodes_random(data, self.aug_ratio)
        elif self.aug == 'dropN_nonC':
            data = drop_nodes_nonC(data, self.aug_ratio)
        elif self.aug == 'none':
            data = data
        elif self.aug == 'dropN_C':
            data = drop_nodes_C(data, self.aug_ratio)
        elif self.aug == 'drop_edge':
            data = drop_edges(data, self.aug_ratio)
        elif self.aug == 'permute_edge':
            data = permute_edges(data, self.aug_ratio)
        elif self.aug == 'mask_node':
            data = mask_attributes(data, self.aug_ratio)
        elif self.aug == 'mask_edge':
            data = mask_attributes(data, self.aug_ratio, mask_edge=True)
        elif self.aug == 'subgraph':
            data = subgraph(data, self.aug_ratio)
        elif self.aug == 'drop_node':
            if self.get_count % every == 0:
                print('[INFO] aug: drop_node')
            data = drop_nodes(data, self.aug_ratio)
        elif self.aug == 'random4':
            ri = np.random.randint(4)
            if ri == 0:
                data = drop_nodes(data, self.aug_ratio)
            elif ri == 1:
                data = subgraph(data, self.aug_ratio)
            elif ri == 2:
                data = permute_edges(data, self.aug_ratio)
            elif ri == 3:
                data = mask_attributes(data, self.aug_ratio)
            else:
                print('sample augmentation error')
                assert False
        elif self.aug == 'DK1':
            if self.get_count % every == 0:
                print('[INFO] aug: DK1')
            data = domain_aug(data, self.original_smiles, self.rule_indicator, self.rules, aug_times=1)
        elif self.aug == 'DK2':
            data = domain_aug(data, self.original_smiles, self.rule_indicator, self.rules, aug_times=2)
        elif self.aug == 'DK3':
            data = domain_aug(data, self.original_smiles, self.rule_indicator, self.rules, aug_times=3)
        elif self.aug == 'DK5':
            data = domain_aug(data, self.original_smiles, self.rule_indicator, self.rules, aug_times=5)
        else:
            print('augmnentation error')

        return data


    @property
    def raw_file_names(self):
        file_name_list = os.listdir(self.raw_dir)
        # assert len(file_name_list) == 1     # currently assume we have a
        # # single raw file
        return file_name_list

    @property
    def processed_file_names(self):
        return 'geometric_data_processed.pt'

    def download(self):
        raise NotImplementedError('Must indicate valid location of raw data. '
                                  'No download allowed')

    def process(self):
        data_smiles_list = []
        data_list = []

        if self.dataset == 'zinc_standard_agent':
            input_path = self.raw_paths[0]
            input_df = pd.read_csv(input_path, sep=',', compression='gzip',
                                   dtype='str')
            smiles_list = list(input_df['smiles'])
            zinc_id_list = list(input_df['zinc_id'])
            for i in range(len(smiles_list)):
                print(i)
                s = smiles_list[i]
                # each example contains a single species
                try:
                    rdkit_mol = AllChem.MolFromSmiles(s)
                    if rdkit_mol != None:  # ignore invalid mol objects
                        # # convert aromatic bonds to double bonds
                        # Chem.SanitizeMol(rdkit_mol,
                        #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                        data = mol_to_graph_data_obj_simple(rdkit_mol)
                        # manually add mol id
                        id = int(zinc_id_list[i].split('ZINC')[1].lstrip('0'))
                        data.id = torch.tensor(
                            [id])  # id here is zinc id value, stripped of
                        # leading zeros
                        data_list.append(data)
                        data_smiles_list.append(smiles_list[i])
                except:
                    continue
        elif self.dataset == 'dti':
            input_path = self.raw_paths[0]
            input_df = pd.read_csv(input_path, sep=',')
            smiles_list = list(input_df['smiles'])
            for i in range(len(smiles_list)):
                print(i)
                s = smiles_list[i]
                rdkit_mol = AllChem.MolFromSmiles(s)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                data.id = torch.tensor([i])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'chembl_filtered':
            ### get downstream test molecules.
            from splitters import scaffold_split

            ### 
            downstream_dir = [
            'dataset/bace',
            'dataset/bbbp',
            'dataset/clintox',
            'dataset/esol',
            'dataset/freesolv',
            'dataset/hiv',
            'dataset/lipophilicity',
            'dataset/muv',
            # 'dataset/pcba/processed/smiles.csv',
            'dataset/sider',
            'dataset/tox21',
            'dataset/toxcast'
            ]

            downstream_inchi_set = set()
            for d_path in downstream_dir:
                print(d_path)
                dataset_name = d_path.split('/')[1]
                downstream_dataset = MoleculeDataset(d_path, dataset=dataset_name)
                downstream_smiles = pd.read_csv(os.path.join(d_path,
                                                             'processed', 'smiles.csv'),
                                                header=None)[0].tolist()

                assert len(downstream_dataset) == len(downstream_smiles)

                _, _, _, (train_smiles, valid_smiles, test_smiles) = scaffold_split(downstream_dataset, downstream_smiles, task_idx=None, null_value=0,
                                   frac_train=0.8,frac_valid=0.1, frac_test=0.1,
                                   return_smiles=True)

                ### remove both test and validation molecules
                remove_smiles = test_smiles + valid_smiles

                downstream_inchis = []
                for smiles in remove_smiles:
                    species_list = smiles.split('.')
                    for s in species_list:  # record inchi for all species, not just
                     # largest (by default in create_standardized_mol_id if input has
                     # multiple species)
                        inchi = create_standardized_mol_id(s)
                        downstream_inchis.append(inchi)
                downstream_inchi_set.update(downstream_inchis)

            smiles_list, rdkit_mol_objs, folds, labels = \
                _load_chembl_with_labels_dataset(os.path.join(self.root, 'raw'))

            print('processing')
            for i in range(len(rdkit_mol_objs)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                if rdkit_mol != None:
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    mw = Descriptors.MolWt(rdkit_mol)
                    if 50 <= mw <= 900:
                        inchi = create_standardized_mol_id(smiles_list[i])
                        if inchi != None and inchi not in downstream_inchi_set:
                            data = mol_to_graph_data_obj_simple(rdkit_mol)
                            # manually add mol id
                            data.id = torch.tensor(
                                [i])  # id here is the index of the mol in
                            # the dataset
                            data.y = torch.tensor(labels[i, :])
                            # fold information
                            if i in folds[0]:
                                data.fold = torch.tensor([0])
                            elif i in folds[1]:
                                data.fold = torch.tensor([1])
                            else:
                                data.fold = torch.tensor([2])
                            data_list.append(data)
                            data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'tox21':
            smiles_list, rdkit_mol_objs, labels = \
                _load_tox21_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                ## convert aromatic bonds to double bonds
                #Chem.SanitizeMol(rdkit_mol,
                                 #sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor(labels[i, :])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'hiv':
            smiles_list, rdkit_mol_objs, labels = \
                _load_hiv_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor([labels[i]])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'bace':
            smiles_list, rdkit_mol_objs, folds, labels = \
                _load_bace_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor([labels[i]])
                data.fold = torch.tensor([folds[i]])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'bbbp':
            smiles_list, rdkit_mol_objs, labels = \
                _load_bbbp_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                if rdkit_mol != None:
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    data = mol_to_graph_data_obj_simple(rdkit_mol)
                    # manually add mol id
                    data.id = torch.tensor(
                        [i])  # id here is the index of the mol in
                    # the dataset
                    data.y = torch.tensor([labels[i]])
                    data_list.append(data)
                    data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'clintox':
            smiles_list, rdkit_mol_objs, labels = \
                _load_clintox_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                if rdkit_mol != None:
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    data = mol_to_graph_data_obj_simple(rdkit_mol)
                    # manually add mol id
                    data.id = torch.tensor(
                        [i])  # id here is the index of the mol in
                    # the dataset
                    data.y = torch.tensor(labels[i, :])
                    data_list.append(data)
                    data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'esol':
            smiles_list, rdkit_mol_objs, labels = \
                _load_esol_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor([labels[i]])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'freesolv':
            smiles_list, rdkit_mol_objs, labels = \
                _load_freesolv_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor([labels[i]])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'lipophilicity':
            smiles_list, rdkit_mol_objs, labels = \
                _load_lipophilicity_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor([labels[i]])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'muv':
            smiles_list, rdkit_mol_objs, labels = \
                _load_muv_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor(labels[i, :])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'pcba':
            smiles_list, rdkit_mol_objs, labels = \
                _load_pcba_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor(labels[i, :])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'pcba_pretrain':
            smiles_list, rdkit_mol_objs, labels = \
                _load_pcba_dataset(self.raw_paths[0])
            downstream_inchi = set(pd.read_csv(os.path.join(self.root,
                                                            'downstream_mol_inchi_may_24_2019'),
                                               sep=',', header=None)[0])
            for i in range(len(smiles_list)):
                print(i)
                if '.' not in smiles_list[i]:   # remove examples with
                    # multiples species
                    rdkit_mol = rdkit_mol_objs[i]
                    mw = Descriptors.MolWt(rdkit_mol)
                    if 50 <= mw <= 900:
                        inchi = create_standardized_mol_id(smiles_list[i])
                        if inchi != None and inchi not in downstream_inchi:
                            # # convert aromatic bonds to double bonds
                            # Chem.SanitizeMol(rdkit_mol,
                            #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                            data = mol_to_graph_data_obj_simple(rdkit_mol)
                            # manually add mol id
                            data.id = torch.tensor(
                                [i])  # id here is the index of the mol in
                            # the dataset
                            data.y = torch.tensor(labels[i, :])
                            data_list.append(data)
                            data_smiles_list.append(smiles_list[i])

        # elif self.dataset == ''

        elif self.dataset == 'sider':
            smiles_list, rdkit_mol_objs, labels = \
                _load_sider_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                # # convert aromatic bonds to double bonds
                # Chem.SanitizeMol(rdkit_mol,
                #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                data = mol_to_graph_data_obj_simple(rdkit_mol)
                # manually add mol id
                data.id = torch.tensor(
                    [i])  # id here is the index of the mol in
                # the dataset
                data.y = torch.tensor(labels[i, :])
                data_list.append(data)
                data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'toxcast':
            smiles_list, rdkit_mol_objs, labels = \
                _load_toxcast_dataset(self.raw_paths[0])
            for i in range(len(smiles_list)):
                print(i)
                rdkit_mol = rdkit_mol_objs[i]
                if rdkit_mol != None:
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    data = mol_to_graph_data_obj_simple(rdkit_mol)
                    # manually add mol id
                    data.id = torch.tensor(
                        [i])  # id here is the index of the mol in
                    # the dataset
                    data.y = torch.tensor(labels[i, :])
                    data_list.append(data)
                    data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'ptc_mr':
            input_path = self.raw_paths[0]
            input_df = pd.read_csv(input_path, sep=',', header=None, names=['id', 'label', 'smiles'])
            smiles_list = input_df['smiles']
            labels = input_df['label'].values
            for i in range(len(smiles_list)):
                print(i)
                s = smiles_list[i]
                rdkit_mol = AllChem.MolFromSmiles(s)
                if rdkit_mol != None:  # ignore invalid mol objects
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    data = mol_to_graph_data_obj_simple(rdkit_mol)
                    # manually add mol id
                    data.id = torch.tensor(
                        [i])
                    data.y = torch.tensor([labels[i]])
                    data_list.append(data)
                    data_smiles_list.append(smiles_list[i])

        elif self.dataset == 'mutag':
            smiles_path = os.path.join(self.root, 'raw', 'mutag_188_data.can')
            # smiles_path = 'dataset/mutag/raw/mutag_188_data.can'
            labels_path = os.path.join(self.root, 'raw', 'mutag_188_target.txt')
            # labels_path = 'dataset/mutag/raw/mutag_188_target.txt'
            smiles_list = pd.read_csv(smiles_path, sep=' ', header=None)[0]
            labels = pd.read_csv(labels_path, header=None)[0].values
            for i in range(len(smiles_list)):
                print(i)
                s = smiles_list[i]
                rdkit_mol = AllChem.MolFromSmiles(s)
                if rdkit_mol != None:  # ignore invalid mol objects
                    # # convert aromatic bonds to double bonds
                    # Chem.SanitizeMol(rdkit_mol,
                    #                  sanitizeOps=Chem.SanitizeFlags.SANITIZE_KEKULIZE)
                    data = mol_to_graph_data_obj_simple(rdkit_mol)
                    # manually add mol id
                    data.id = torch.tensor(
                        [i])
                    data.y = torch.tensor([labels[i]])
                    data_list.append(data)
                    data_smiles_list.append(smiles_list[i])
                    

        else:
            raise ValueError('Invalid dataset name')

        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]

        # write data_smiles_list in processed paths
        data_smiles_series = pd.Series(data_smiles_list)
        data_smiles_series.to_csv(os.path.join(self.processed_dir,
                                               'smiles.csv'), index=False,
                                  header=False)

        data, slices = self.collate(data_list)
        torch.save((data, slices), self.processed_paths[0])

def _load_tox21_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    tasks = ['NR-AR', 'NR-AR-LBD', 'NR-AhR', 'NR-Aromatase', 'NR-ER', 'NR-ER-LBD',
       'NR-PPAR-gamma', 'SR-ARE', 'SR-ATAD5', 'SR-HSE', 'SR-MMP', 'SR-p53']
    labels = input_df[tasks]
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # convert nan to 0
    labels = labels.fillna(0)
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values

def _load_hiv_dataset(input_path):
    """
    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    labels = input_df['HIV_active']
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # there are no nans
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values

def _load_bace_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array
    containing indices for each of the 3 folds, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['mol']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    labels = input_df['Class']
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # there are no nans
    folds = input_df['Model']
    folds = folds.replace('Train', 0)   # 0 -> train
    folds = folds.replace('Valid', 1)   # 1 -> valid
    folds = folds.replace('Test', 2)    # 2 -> test
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    assert len(smiles_list) == len(folds)
    return smiles_list, rdkit_mol_objs_list, folds.values, labels.values

def _load_bbbp_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]

    preprocessed_rdkit_mol_objs_list = [m if m != None else None for m in
                                                          rdkit_mol_objs_list]
    preprocessed_smiles_list = [AllChem.MolToSmiles(m) if m != None else
                                None for m in preprocessed_rdkit_mol_objs_list]
    labels = input_df['p_np']
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # there are no nans
    assert len(smiles_list) == len(preprocessed_rdkit_mol_objs_list)
    assert len(smiles_list) == len(preprocessed_smiles_list)
    assert len(smiles_list) == len(labels)
    return preprocessed_smiles_list, preprocessed_rdkit_mol_objs_list, \
           labels.values

def _load_clintox_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]

    preprocessed_rdkit_mol_objs_list = [m if m != None else None for m in
                                        rdkit_mol_objs_list]
    preprocessed_smiles_list = [AllChem.MolToSmiles(m) if m != None else
                                None for m in preprocessed_rdkit_mol_objs_list]
    tasks = ['FDA_APPROVED', 'CT_TOX']
    labels = input_df[tasks]
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # there are no nans
    assert len(smiles_list) == len(preprocessed_rdkit_mol_objs_list)
    assert len(smiles_list) == len(preprocessed_smiles_list)
    assert len(smiles_list) == len(labels)
    return preprocessed_smiles_list, preprocessed_rdkit_mol_objs_list, \
           labels.values
# input_path = 'dataset/clintox/raw/clintox.csv'
# smiles_list, rdkit_mol_objs_list, labels = _load_clintox_dataset(input_path)

def _load_esol_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels (regression task)
    """
    # NB: some examples have multiple species
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    labels = input_df['measured log solubility in mols per litre']
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values
# input_path = 'dataset/esol/raw/delaney-processed.csv'
# smiles_list, rdkit_mol_objs_list, labels = _load_esol_dataset(input_path)

def _load_freesolv_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels (regression task)
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    labels = input_df['expt']
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values


def _load_lipophilicity_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels (regression task)
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    labels = input_df['exp']
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values


def _load_muv_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    tasks = ['MUV-466', 'MUV-548', 'MUV-600', 'MUV-644', 'MUV-652', 'MUV-689',
       'MUV-692', 'MUV-712', 'MUV-713', 'MUV-733', 'MUV-737', 'MUV-810',
       'MUV-832', 'MUV-846', 'MUV-852', 'MUV-858', 'MUV-859']
    labels = input_df[tasks]
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # convert nan to 0
    labels = labels.fillna(0)
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.values

def _load_sider_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    tasks = ['Hepatobiliary disorders',
       'Metabolism and nutrition disorders', 'Product issues', 'Eye disorders',
       'Investigations', 'Musculoskeletal and connective tissue disorders',
       'Gastrointestinal disorders', 'Social circumstances',
       'Immune system disorders', 'Reproductive system and breast disorders',
       'Neoplasms benign, malignant and unspecified (incl cysts and polyps)',
       'General disorders and administration site conditions',
       'Endocrine disorders', 'Surgical and medical procedures',
       'Vascular disorders', 'Blood and lymphatic system disorders',
       'Skin and subcutaneous tissue disorders',
       'Congenital, familial and genetic disorders',
       'Infections and infestations',
       'Respiratory, thoracic and mediastinal disorders',
       'Psychiatric disorders', 'Renal and urinary disorders',
       'Pregnancy, puerperium and perinatal conditions',
       'Ear and labyrinth disorders', 'Cardiac disorders',
       'Nervous system disorders',
       'Injury, poisoning and procedural complications']
    labels = input_df[tasks]
    # convert 0 to -1
    labels = labels.replace(0, -1)
    assert len(smiles_list) == len(rdkit_mol_objs_list)
    assert len(smiles_list) == len(labels)
    return smiles_list, rdkit_mol_objs_list, labels.value

def _load_toxcast_dataset(input_path):
    """

    :param input_path:
    :return: list of smiles, list of rdkit mol obj, np.array containing the
    labels
    """
    # NB: some examples have multiple species, some example smiles are invalid
    input_df = pd.read_csv(input_path, sep=',')
    smiles_list = input_df['smiles']
    rdkit_mol_objs_list = [AllChem.MolFromSmiles(s) for s in smiles_list]
    # Some smiles could not be successfully converted
    # to rdkit mol object so them to None
    preprocessed_rdkit_mol_objs_list = [m if m != None else None for m in
                                        rdkit_mol_objs_list]
    preprocessed_smiles_list = [AllChem.MolToSmiles(m) if m != None else
                                None for m in preprocessed_rdkit_mol_objs_list]
    tasks = list(input_df.columns)[1:]
    labels = input_df[tasks]
    # convert 0 to -1
    labels = labels.replace(0, -1)
    # convert nan to 0
    labels = labels.fillna(0)
    assert len(smiles_list) == len(preprocessed_rdkit_mol_objs_list)
    assert len(smiles_list) == len(preprocessed_smiles_list)
    assert len(smiles_list) == len(labels)
    return preprocessed_smiles_list, preprocessed_rdkit_mol_objs_list, \
           labels.values

def _load_chembl_with_labels_dataset(root_path):
    """
    Data from 'Large-scale comparison of machine learning methods for drug target prediction on ChEMBL'
    :param root_path: path to the folder containing the reduced chembl dataset
    :return: list of smiles, preprocessed rdkit mol obj list, list of np.array
    containing indices for each of the 3 folds, np.array containing the labels
    """
    # adapted from https://github.com/ml-jku/lsc/blob/master/pythonCode/lstm/loadData.py
    # first need to download the files and unzip:
    # wget http://bioinf.jku.at/research/lsc/chembl20/dataPythonReduced.zip
    # unzip and rename to chembl_with_labels
    # wget http://bioinf.jku.at/research/lsc/chembl20/dataPythonReduced/chembl20Smiles.pckl
    # into the dataPythonReduced directory
    # wget http://bioinf.jku.at/research/lsc/chembl20/dataPythonReduced/chembl20LSTM.pckl

    # 1. load folds and labels
    f=open(os.path.join(root_path, 'folds0.pckl'), 'rb')
    folds=pickle.load(f)
    f.close()

    f=open(os.path.join(root_path, 'labelsHard.pckl'), 'rb')
    targetMat=pickle.load(f)
    sampleAnnInd=pickle.load(f)
    targetAnnInd=pickle.load(f)
    f.close()

    targetMat=targetMat
    targetMat=targetMat.copy().tocsr()
    targetMat.sort_indices()
    targetAnnInd=targetAnnInd
    targetAnnInd=targetAnnInd-targetAnnInd.min()

    folds=[np.intersect1d(fold, sampleAnnInd.index.values).tolist() for fold in folds]
    targetMatTransposed=targetMat[sampleAnnInd[list(chain(*folds))]].T.tocsr()
    targetMatTransposed.sort_indices()
    # # num positive examples in each of the 1310 targets
    trainPosOverall=np.array([np.sum(targetMatTransposed[x].data > 0.5) for x in range(targetMatTransposed.shape[0])])
    # # num negative examples in each of the 1310 targets
    trainNegOverall=np.array([np.sum(targetMatTransposed[x].data < -0.5) for x in range(targetMatTransposed.shape[0])])
    # dense array containing the labels for the 456331 molecules and 1310 targets
    denseOutputData=targetMat.A # possible values are {-1, 0, 1}

    # 2. load structures
    f=open(os.path.join(root_path, 'chembl20LSTM.pckl'), 'rb')
    rdkitArr=pickle.load(f)
    f.close()

    assert len(rdkitArr) == denseOutputData.shape[0]
    assert len(rdkitArr) == len(folds[0]) + len(folds[1]) + len(folds[2])

    preprocessed_rdkitArr = []
    print('preprocessing')
    for i in range(len(rdkitArr)):
        print(i)
        m = rdkitArr[i]
        if m == None:
            preprocessed_rdkitArr.append(None)
        else:
            mol_species_list = split_rdkit_mol_obj(m)
            if len(mol_species_list) == 0:
                preprocessed_rdkitArr.append(None)
            else:
                largest_mol = get_largest_mol(mol_species_list)
                if len(largest_mol.GetAtoms()) <= 2:
                    preprocessed_rdkitArr.append(None)
                else:
                    preprocessed_rdkitArr.append(largest_mol)

    assert len(preprocessed_rdkitArr) == denseOutputData.shape[0]

    smiles_list = [AllChem.MolToSmiles(m) if m != None else None for m in
                   preprocessed_rdkitArr]   # bc some empty mol in the
    # rdkitArr zzz...

    assert len(preprocessed_rdkitArr) == len(smiles_list)

    return smiles_list, preprocessed_rdkitArr, folds, denseOutputData
# root_path = 'dataset/chembl_with_labels'

def check_smiles_validity(smiles):
    try:
        m = Chem.MolFromSmiles(smiles)
        if m:
            return True
        else:
            return False
    except:
        return False

def split_rdkit_mol_obj(mol):
    """
    Split rdkit mol object containing multiple species or one species into a
    list of mol objects or a list containing a single object respectively
    :param mol:
    :return:
    """
    smiles = AllChem.MolToSmiles(mol, isomericSmiles=True)
    smiles_list = smiles.split('.')
    mol_species_list = []
    for s in smiles_list:
        if check_smiles_validity(s):
            mol_species_list.append(AllChem.MolFromSmiles(s))
    return mol_species_list

def get_largest_mol(mol_list):
    """
    Given a list of rdkit mol objects, returns mol object containing the
    largest num of atoms. If multiple containing largest num of atoms,
    picks the first one
    :param mol_list:
    :return:
    """
    num_atoms_list = [len(m.GetAtoms()) for m in mol_list]
    largest_mol_idx = num_atoms_list.index(max(num_atoms_list))
    return mol_list[largest_mol_idx]

def create_all_datasets():
    #### create dataset
    downstream_dir = [
            'bace',
            'bbbp',
            'clintox',
            'esol',
            'freesolv',
            'hiv',
            'lipophilicity',
            'muv',
            'sider',
            'tox21',
            'toxcast'
            ]

    for dataset_name in downstream_dir:
        print(dataset_name)
        root = "dataset/" + dataset_name
        os.makedirs(root + "/processed", exist_ok=True)
        dataset = MoleculeDataset(root, dataset=dataset_name)
        print(dataset)


    dataset = MoleculeDataset(root = "dataset/chembl_filtered", dataset="chembl_filtered")
    print(dataset)
    dataset = MoleculeDataset(root = "dataset/zinc_standard_agent", dataset="zinc_standard_agent")
    print(dataset)


# test MoleculeDataset object
if __name__ == "__main__":

    s = 'CCC(C)=O'
    mol = AllChem.MolFromSmiles(s)
    num_atom_features = 2   # atom type,  chirality tag
    atom_features_list = []
    for atom in mol.GetAtoms():
        # atom feature [-1, 2]
        atom_feature = [allowable_features['possible_atomic_num_list'].index(
            atom.GetAtomicNum())] + [allowable_features[
            'possible_chirality_list'].index(atom.GetChiralTag())]
        #print(atom_feature)
        atom_features_list.append(atom_feature)
    x = torch.tensor(np.array(atom_features_list), dtype=torch.long)
    print(x)

    # bonds
    num_bond_features = 2   # bond type, bond direction
    if len(mol.GetBonds()) > 0: # mol has bonds
        edges_list = []
        edge_features_list = []
        for bond in mol.GetBonds():
            i = bond.GetBeginAtomIdx()
            j = bond.GetEndAtomIdx()
            edge_feature = [allowable_features['possible_bonds'].index(
                bond.GetBondType())] + [allowable_features[
                                            'possible_bond_dirs'].index(
                bond.GetBondDir())]
            edges_list.append((i, j))
            edge_features_list.append(edge_feature)
            edges_list.append((j, i))
            edge_features_list.append(edge_feature)
        print(edges_list)
        print(edge_features_list)

        # data.edge_index: Graph connectivity in COO format with shape [2, num_edges]
        # COO format: fast format for constructing sparse matrices
        edge_index = torch.tensor(np.array(edges_list).T, dtype=torch.long)

        # data.edge_attr: Edge feature matrix with shape [num_edges, num_edge_features]
        edge_attr = torch.tensor(np.array(edge_features_list),
                                 dtype=torch.long)
    else:   # mol has no bonds
        edge_index = torch.empty((2, 0), dtype=torch.long)
        edge_attr = torch.empty((0, num_bond_features), dtype=torch.long)

    # x: [num_atoms, num_atom_feature], edge_index: [2, num_edges], edge_attr: [num_edges, num_edge_features]
    data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
    data.id = 0
    data.y = 2
    print(data)
    print(data.x)
    print(data.edge_attr)
    print(data.edge_index)

    # node attr does not delete, edge_index deleted, how about edge attr?
    #drop_nodes_random(data, 0.4)
    # drop_nodes_nonC(data, 0.4)
    # permute_edges(data, 0.4)
    subgraph(data, 0.4)
    # mask_nodes(data, 0.4)
    # drop_nodes(data, 0.4)

    # df = MoleculeDataset('pretrain_dataset/tox21', dataset='tox21', empty=False)
    # df.get(0)
