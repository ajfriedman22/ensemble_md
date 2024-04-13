####################################################################
#                                                                  #
#    ensemble_md,                                                  #
#    a python package for running GROMACS simulation ensembles     #
#                                                                  #
#    Written by Wei-Tse Hsu <wehs7661@colorado.edu>                #
#    Copyright (c) 2022 University of Colorado Boulder             #
#                                                                  #
####################################################################
import numpy as np
import matplotlib.pyplot as plt
from itertools import combinations
from ensemble_md.utils.utils import run_gmx_cmd
from ensemble_md.analysis import analyze_traj


def cluster_traj(gmx_executable, inputs, grps, coupled_only=True, method='linkage', cutoff=0.1, suffix=None):
    """
    Performs clustering analysis on a trajectory using the GROMACS command :code:`gmx cluster`.
    Note that only fully coupled configurations are considered.

    Parameters
    ----------
    gmx_executable : str
        The path to the GROMACS executable.
    inputs : dict
        A dictionary that contains the different input files required for the clustering analysis.
        The dictionary must have the following four keys: :code:`traj` (input trajectory file in
        XTC or TRR format), :code:`config` (the configuration file in TPR or GRO format),
        :code:`xvg` (a GROMACS XVG file), and :code:`index` (an index/NDX file), with the values
        being the paths. Note that the value of the key :code:`index` can be :code:`None`, in which
        case the function will use a default index file generated by :code:`gmx make_ndx`. If the
        parameter :code:`coupled_only` is set to :code:`True`, an XVG file that contains the time
        series of the state index (e.g., :code:`dhdl.xvg`) must be provided with the key :code:`xvg`.
        Otherwise, the key :code:`xvg` can be set to :code:`None`.
    grps : dict
        A dictionary that contains the names of the groups in the index file (NDX) for
        centering the system, calculating the RMSD, and outputting. The keys are
        :code:`center`, :code:`rmsd`, and :code:`output`.
    coupled_only : bool
        Whether to consider only the fully coupled configurations. The default is :code:`True`.
    method : str
        The method for clustering available for the GROMACS command :code:`gmx cluster`. The default is 'linkage'.
        Check the GROMACS documentation for other available options.
    cutoff : float
        The RMSD cutoff for clustering in nm. The default is 0.1.
    suffix : str
        The suffix for the output files. The default is :code:`None`, which means no suffix will be added.
    """
    # Check if the index file is provided
    if inputs['index'] is None:
        print('Running gmx make_ndx to generate an index file ...')
        args = [
            gmx_executable, 'make_ndx',
            '-f', inputs['config'],
            '-o', 'index.ndx',
        ]
        returncode, stdout, stderr = run_gmx_cmd(args, prompt_input='q\n')
        inputs['index'] = 'index.ndx'

    # Check if the groups are present in the index file
    with open(inputs['index'], 'r') as f:
        content = f.read()
    for key in grps:
        if grps[key] not in content:
            raise ValueError(f'The group {grps[key]} is not present in the provided/generated index file.')

    outputs = {
        'nojump': 'nojump.xtc',
        'center': 'center.xtc',
        'rmsd-clust': 'rmsd-clust.xpm',
        'rmsd-dist': 'rmsd-dist.xvg',
        'cluster-log': 'cluster.log',
        'cluster-pdb': 'clusters.pdb',
        'rmsd': 'rmsd.xvg',  # inter-medoid RMSD
    }
    if suffix is not None:
        for key in outputs:
            outputs[key] = outputs[key].replace('.', f'_{suffix}.')

    # Check if there is any fully coupled state in the trajectory
    lambda_data = np.transpose(np.loadtxt(inputs['xvg'], comments=['#', '@']))[1]
    if coupled_only is True and 0 not in lambda_data:
        print('Terminating clustering analysis since no fully decoupled state is present in the input trajectory while coupled_only is set to True.')  # noqa: E501
    else:
        # Either coupled_only is False or coupled_only is True but there are coupled configurations.
        print('Eliminating jumps across periodic boundaries for the input trajectory ...')
        args = [
            gmx_executable, 'trjconv',
            '-f', inputs['traj'],
            '-s', inputs['config'],
            '-n', inputs['index'],
            '-o', outputs['nojump'],
            '-center', 'yes',
            '-pbc', 'nojump',
        ]

        if coupled_only:
            if inputs['xvg'] is None:
                raise ValueError('The parameter "coupled_only" is set to True but no XVG file is provided.')
            args.extend([
                '-drop', inputs['xvg'],
                '-dropover', '0'
            ])

        returncode, stdout, stderr = run_gmx_cmd(args, prompt_input=f'{grps["center"]}\n{grps["output"]}\n')
        if returncode != 0:
            print(f'Error with return code: {returncode}):\n{stderr}')

        print('Centering the system ...')
        args = [
            gmx_executable, 'trjconv',
            '-f', outputs['nojump'],
            '-s', inputs['config'],
            '-n', inputs['index'],
            '-o', outputs['center'],
            '-center', 'yes',
            '-pbc', 'mol',
            '-ur', 'compact',
        ]
        returncode, stdout, stderr = run_gmx_cmd(args, prompt_input=f'{grps["center"]}\n{grps["output"]}\n')
        if returncode != 0:
            print(f'Error with return code: {returncode}):\n{stderr}')

        if coupled_only is True:
            N_coupled = np.count_nonzero(lambda_data == 0)
            print(f'Number of fully coupled configurations: {N_coupled}')

        print('Performing clustering analysis ...')
        args = [
            gmx_executable, 'cluster',
            '-f', outputs['center'],
            '-s', inputs['config'],
            '-n', inputs['index'],
            '-o', outputs['rmsd-clust'],
            '-dist', outputs['rmsd-dist'],
            '-g', outputs['cluster-log'],
            '-cl', outputs['cluster-pdb'],
            '-cutoff', str(cutoff),
            '-method', method,
        ]
        returncode, stdout, stderr = run_gmx_cmd(args, prompt_input=f'{grps["rmsd"]}\n{grps["output"]}\n')
        if returncode != 0:
            print(f'Error with return code: {returncode}):\n{stderr}')

        rmsd_range, rmsd_avg, n_clusters = get_cluster_info(outputs['cluster-log'])

        print(f'Range of RMSD values: from {rmsd_range[0]:.3f} to {rmsd_range[1]:.3f} nm')
        print(f'Average RMSD: {rmsd_avg:.3f} nm')
        print(f'Number of clusters: {n_clusters}')

        if n_clusters > 1:
            clusters, sizes = get_cluster_members(outputs['cluster-log'])
            for i in range(1, n_clusters + 1):
                print(f'  - Cluster {i} accounts for {sizes[i] * 100:.2f}% of the total configurations.')
            
            if n_clusters == 2:
                # n_transitions, t_transitions = count_transitions(clusters)
                transmtx, _ = get_cluster_transmtx(clusters, normalize=False)  # Note that this is a 2D count matrix.
                n_transitions = np.sum(transmtx) - np.trace(transmtx)  # This is the sum of all off-diagonal elements. np.trace calculates the sum of the diagonal elements.
                print(f'Number of transitions between the two clusters: {n_transitions}')
                print(f'Time frames of the transitions (ps): {t_transitions}')

            print('Calculating the inter-medoid RMSD between the two biggest clusters ...')
            # Note that we pass outputs['cluster-pdb'] to -s so that the first medoid will be used as the reference
            args = [
                gmx_executable, 'rms',
                '-f', outputs['cluster-pdb'],
                '-s', outputs['cluster-pdb'],
                '-o', outputs['rmsd'],
            ]
            if inputs['index'] is not None:
                args.extend(['-n', inputs['index']])

            # Here we simply assume same groups for least-squares fitting and RMSD calculation
            returncode, stdout, stderr = run_gmx_cmd(args, prompt_input=f'{grps["rmsd"]}\n{grps["rmsd"]}\n')
            if returncode != 0:
                print(f'Error with return code: {returncode}):\n{stderr}')

            rmsd = np.transpose(np.loadtxt(outputs['rmsd'], comments=['@', '#']))[1][1]  # inter-medoid RMSD
            print(f'Inter-medoid RMSD between the two biggest clusters: {rmsd:.3f} nm')


def get_cluster_info(cluster_log):
    """
    Gets the metadata of the LOG file generated by the GROMACS :code:`gmx cluster` command.

    Parameters
    ----------
    cluster_log : str
        The LOG file generated by the GROMACS :code:`gmx cluster` command.

    Returns
    -------
    rmsd_range: list
        The range of RMSD values
    rmsd_avg: float
        The average RMSD value.
    n_clusters : int
        The number of clusters.
    """
    f = open(cluster_log, 'r')
    lines = f.readlines()
    f.close()

    rmsd_range = []
    for line in lines:
        if 'The RMSD ranges from' in line:
            rmsd_range.append(float(line.split('from')[-1].split('to')[0]))
            rmsd_range.append(float(line.split('from')[-1].split('to')[-1].split('nm')[0]))
        if 'Average RMSD' in line:
            rmsd_avg = float(line.split('is')[-1])
        if 'Found' in line:
            n_clusters = int(line.split()[1])
            break

    return rmsd_range, rmsd_avg, n_clusters


def get_cluster_members(cluster_log):
    """
    Gets the members of each cluster from the LOG file generated by the GROMACS :code:`gmx cluster` command.

    Parameters
    ----------
    cluster_log : str
        The LOG file generated by the GROMACS :code:`gmx cluster` command.

    Returns
    -------
    clusters : dict
        A dictionary that contains the cluster index (starting from 1) as the key and the list of members
        (configurations at different timeframes) as the value.
    sizes : dict
        A dictionary that contains the cluster index (starting from 1) as the key and the size of the cluster
        (in fraction) as the value.
    """
    clusters = {}
    current_cluster = 0
    start_processing = False

    f = open(cluster_log, 'r')
    lines = f.readlines()
    f.close()

    for line in lines:
        # Start processing when we reach the line that starts with "cl."
        if line.strip().startswith("cl."):
            start_processing = True
            continue  # Skip this line and continue to the next iteration

        if start_processing:
            parts = line.split('|')
            try:
                current_cluster = int(parts[0].strip())
                clusters[current_cluster] = []
            except ValueError:
                pass

            # This is either a new cluster or continuation of it, add members
            members = parts[-1].split()
            clusters[current_cluster].extend([int(i) for i in members])

    sizes_list = [len(clusters[i]) for i in clusters]
    sizes = {i: sizes_list[i - 1] / sum(sizes_list) for i in clusters}

    return clusters, sizes


def count_transitions(clusters, idx_1=1, idx_2=2):
    """
    Counts the number of transitions between two specified clusters.

    Parameters
    ----------
    clusters : dict
        A dictionary that contains the cluster index (starting from 1) as the key and the list of members
        (configurations at different timeframes) as the value.
    idx_1 : int
        The index of a cluster of interst.
    idx_2 : int
        The index of the other cluster of interest.

    Returns
    -------
    n_transitions : int
        The number of transitions between the two biggest clusters.
    t_transitions : list
        The list of time frames when the transitions occur. Note that if there was no transition (i.e., only one
        cluster), an empty list will be returned.
    """
    if len(clusters) < 2:
        return 0, []

    # Combine and sort all cluster members for the first two biggest clusters while keeping track of their origin
    all_members = [(member, idx_1) for member in clusters[idx_1]] + [(member, idx_2) for member in clusters[idx_2]]
    all_members.sort()

    # Count transitions and record time frames
    n_transitions = 0
    t_transitions = []
    last_cluster = all_members[0][1]  # the cluster index of the last time frame in the previous iteration

    for member in all_members[1:]:
        if member[1] != last_cluster:
            n_transitions += 1
            last_cluster = member[1]
            t_transitions.append(member[0])

    return n_transitions, t_transitions


def analyze_transitions(clusters, normalize=True, plot_type=None):
    """
    Analyzes transitions between clusters, including estimating the transition matrix, generating/plotting a trajectory
    showing which cluster each configuration belongs to, and/or plotting the distribution of the clusters.

    Parameters
    ----------
    clusters : dict
        A dictionary that contains the cluster index (starting from 1) as the key and the list of members
        (configurations at different timeframes in ps) as the value.
    plot_type : str
        The type of the figure to be plotted. The default is :code:`None`, which means no figure will be plotted.
        The other options are :code:`'bar'` and :code:`'xy'`. The former plots the distribution of the clusters,
        while the latter plots the trajectory showing which cluster each configuration belongs to.

    Returns
    -------
    transmtx: np.ndarray
        The transition matrix.
    traj: np.ndarray
        The trajectory showing which cluster each configuration belongs to.
    t_transitions: dict
        A dictionary with keys being pairs of cluster indices and values being the time frames of transitions
        between the two clusters.
    """
    # Combine all cluster members and sort them
    all_members = []
    for key in clusters:
        all_members.extend([(member, key) for member in clusters[key]])
    all_members.sort()

    # Generate the trajectory
    t = np.array([member[0] for member in all_members])
    traj = np.array([member[1] for member in all_members])

    # Generate the transition matrix
    # Since traj2transmtx assumes an index starting from 0, we subtract 1 from the trajectory
    transmtx = analyze_traj.traj2transmtx(traj - 1, len(clusters), normalize=normalize)

    # Generate the dictionary of transitions
    t_transitions = {}
    for i in range(len(traj) - 1):
        if traj[i] != traj[i + 1]:
            pair = tuple(sorted((traj[i], traj[i + 1])))
            if pair not in t_transitions:
                t_transitions[pair] = [t[i]]
            else:
                t_transitions[pair].append(t[i])

    if plot_type is not None:
        if plot_type == 'bar':
            fig = plt.figure()
            ax = fig.add_subplot(111)
            plt.bar(clusters.keys(), [len(clusters[i]) for i in clusters], width=0.35)
            plt.xlabel('Cluster index')
            plt.ylabel('Number of configurations')
            plt.grid()
            ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
            ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
            plt.savefig('cluster_distribution.png', dpi=600)
        elif plot_type == 'xy':
            fig = plt.figure()
            ax = fig.add_subplot(111)
            plt.plot(t, traj)
            if len(t) > 1000:
                t /= 1000  # convert to ns
                units = 'ns'
            else:
                units = 'ps'
            plt.xlabel(f'Time frame ({units})')
            plt.ylabel('Cluster index')
            ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
            plt.grid()
            plt.savefig('cluster_traj.png', dpi=600)
        else:
            raise ValueError(f'Invalid plot type: {plot_type}. The plot type must be either "bar" or "xy" or unspecified.')  # noqa: E501

    return transmtx, traj, t_transitions
