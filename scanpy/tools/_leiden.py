from __future__ import annotations

import random
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from natsort import natsorted

from .. import _utils
from .. import logging as logg
from ._utils_clustering import rename_groups, restrict_adjacency

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anndata import AnnData
    from scipy import sparse

try:
    from leidenalg.VertexPartition import MutableVertexPartition
except ImportError:

    class MutableVertexPartition:
        pass

    MutableVertexPartition.__module__ = "leidenalg.VertexPartition"


def leiden(
    adata: AnnData,
    resolution: float = 1,
    *,
    restrict_to: tuple[str, Sequence[str]] | None = None,
    random_state: _utils.AnyRandom = 0,
    key_added: str = "leiden",
    adjacency: sparse.spmatrix | None = None,
    directed: bool = False,
    use_weights: bool = True,
    n_iterations: int = 2,
    partition_type: type[MutableVertexPartition] | None = None,
    neighbors_key: str | None = None,
    obsp: str | None = None,
    copy: bool = False,
    use_leidenalg: bool = False,
    **clustering_args,
) -> AnnData | None:
    """\
    Cluster cells into subgroups [Traag18]_.

    Cluster cells using the Leiden algorithm [Traag18]_,
    an improved version of the Louvain algorithm [Blondel08]_.
    It has been proposed for single-cell analysis by [Levine15]_.

    This requires having ran :func:`~scanpy.pp.neighbors` or
    :func:`~scanpy.external.pp.bbknn` first.

    Parameters
    ----------
    adata
        The annotated data matrix.
    resolution
        A parameter value controlling the coarseness of the clustering.
        Higher values lead to more clusters.
        Set to `None` if overriding `partition_type`
        to one that doesn’t accept a `resolution_parameter`.
    random_state
        Change the initialization of the optimization.
    restrict_to
        Restrict the clustering to the categories within the key for sample
        annotation, tuple needs to contain `(obs_key, list_of_categories)`.
    key_added
        `adata.obs` key under which to add the cluster labels.
    adjacency
        Sparse adjacency matrix of the graph, defaults to neighbors connectivities.
    directed
        Whether to treat the graph as directed or undirected.
    use_weights
        If `True`, edge weights from the graph are used in the computation
        (placing more emphasis on stronger edges).
    n_iterations
        How many iterations of the Leiden clustering algorithm to perform.
        Positive values above 2 define the total number of iterations to perform,
        -1 has the algorithm run until it reaches its optimal clustering.
    partition_type
        Type of partition to use.
        Defaults to :class:`~leidenalg.RBConfigurationVertexPartition`.
        For the available options, consult the documentation for
        :func:`~leidenalg.find_partition`.
    neighbors_key
        Use neighbors connectivities as adjacency.
        If not specified, leiden looks .obsp['connectivities'] for connectivities
        (default storage place for pp.neighbors).
        If specified, leiden looks
        .obsp[.uns[neighbors_key]['connectivities_key']] for connectivities.
    obsp
        Use .obsp[obsp] as adjacency. You can't specify both
        `obsp` and `neighbors_key` at the same time.
    copy
        Whether to copy `adata` or modify it inplace.
    use_leidenalg
        Whether or not to use the `leidenalg` leiden implementation.
    **clustering_args
        Any further arguments to pass to `~leidenalg.find_partition` (which in turn passes arguments to the `partition_type`)
        or `community_detection` from `igraph`.

    Returns
    -------
    Returns `None` if `copy=False`, else returns an `AnnData` object. Sets the following fields:

    `adata.obs['leiden' | key_added]` : :class:`pandas.Series` (dtype ``category``)
        Array of dim (number of samples) that stores the subgroup id
        (``'0'``, ``'1'``, ...) for each cell.

    `adata.uns['leiden']['params']` : :class:`dict`
        A dict with the values for the parameters `resolution`, `random_state`,
        and `n_iterations`.
    """
    if use_leidenalg:
        try:
            import leidenalg
        except ImportError:
            raise ImportError(
                "Please install the leiden algorithm: `conda install -c conda-forge leidenalg` or `pip3 install leidenalg`."
            )
    else:
        try:
            import igraph
        except ImportError:
            raise ImportError(
                "Please install igraph: `conda install -c conda-forge igraph` or `pip3 install igraph`."
            )
    clustering_args = dict(clustering_args)

    start = logg.info("running Leiden clustering")
    adata = adata.copy() if copy else adata
    # are we clustering a user-provided graph or the default AnnData one?
    if adjacency is None:
        adjacency = _utils._choose_graph(adata, obsp, neighbors_key)
    if restrict_to is not None:
        restrict_key, restrict_categories = restrict_to
        adjacency, restrict_indices = restrict_adjacency(
            adata,
            restrict_key,
            restrict_categories=restrict_categories,
            adjacency=adjacency,
        )
    # convert it to igraph
    if not use_leidenalg and directed:
        raise ValueError(
            "Cannot use igraph's leiden implemntation with a directed graph."
        )
    g = _utils.get_igraph_from_adjacency(adjacency, directed=directed)
    # flip to the default partition type if not overriden by the user
    if partition_type is None and use_leidenalg:
        partition_type = leidenalg.RBConfigurationVertexPartition
    elif not use_leidenalg and partition_type is not None:
        raise ValueError("Do not pass in partition_type argument when using igraph.")
    # Prepare find_partition arguments as a dictionary,
    # appending to whatever the user provided. It needs to be this way
    # as this allows for the accounting of a None resolution
    # (in the case of a partition variant that doesn't take it on input)
    if use_weights:
        clustering_args["weights"] = (
            "weight"
            if not use_leidenalg
            else np.array(g.es["weight"]).astype(np.float64)
        )
    clustering_args["n_iterations"] = n_iterations
    if use_leidenalg:
        clustering_args["seed"] = random_state
    else:
        random.seed(random_state)
        igraph.set_random_number_generator(random)
    if resolution is not None:
        clustering_args[
            f"resolution{'_parameter' if use_leidenalg else ''}"
        ] = resolution
    # clustering proper
    if not use_leidenalg:
        part = g.community_leiden(objective_function="modularity", **clustering_args)
    else:
        part = leidenalg.find_partition(g, partition_type, **clustering_args)
    # store output into adata.obs
    groups = np.array(part.membership)
    if restrict_to is not None:
        if key_added == "leiden":
            key_added += "_R"
        groups = rename_groups(
            adata,
            key_added=key_added,
            restrict_key=restrict_key,
            restrict_categories=restrict_categories,
            restrict_indices=restrict_indices,
            groups=groups,
        )
    adata.obs[key_added] = pd.Categorical(
        values=groups.astype("U"),
        categories=natsorted(map(str, np.unique(groups))),
    )
    # store information on the clustering parameters
    adata.uns["leiden"] = {}
    adata.uns["leiden"]["params"] = dict(
        resolution=resolution,
        random_state=random_state,
        n_iterations=n_iterations,
    )
    logg.info(
        "    finished",
        time=start,
        deep=(
            f"found {len(np.unique(groups))} clusters and added\n"
            f"    {key_added!r}, the cluster labels (adata.obs, categorical)"
        ),
    )
    return adata if copy else None
