import os
import vtk
import json
import psutil
import pickle
import argparse
import warnings
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from aicsshparam import shtools
from aicscytoparam import cytoparam
from aicsimageio import AICSImage, writers
from typing import Dict, List, Optional, Union
from aics_dask_utils import DistributedHandler
from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk
import concurrent

from cvapipe_analysis.tools import io, general, controller

class Aggregator(io.DataProducer):
    """
    The goal of this class is to have a combination of
    parameters as input, including some CellIds. The
    corresponding cells have their parameterized intensity
    representation morphed into the appropriated shape
    space shape according to the input parameters.

    WARNING: All classes are assumed to know the whole
    structure of directories inside the local_staging
    folder and this is hard coded. Therefore, classes
    may break if you move saved files away from the
    places their are saved.
    """

    def __init__(self, config):
        super().__init__(config)
        self.subfolder = 'aggregation/aggmorph'

    def workflow(self):
        self.set_agg_function()
        self.aggregate_parameterized_intensities()
        self.morph_on_shapemode_shape()

    def get_output_file_name(self):
        return f"{self.get_prefix_from_row(self.row)}.tif"

    def save(self):
        img = self.morphed
        n = len(self.CellIds)
        save_as = self.get_output_file_path()
        self.write_ome_tif(
            save_as, img, ['domain', save_as.stem], f"N{n}")
        img = self.aggregated_parameterized_intensity
        img = img.reshape(1, *img.shape)
        save_as = Path(str(save_as).replace('aggmorph', 'repsagg'))
        self.write_ome_tif(
            save_as, img, [save_as.stem], f"N{n}")
        return save_as

    def set_shape_space(self, space):
        self.space = space
        self.load_parameterization_manifest()

    def aggregate_parameterized_intensities(self):
        nc = self.control.get_ncores()
        if not len(self.CellIds):
            raise ValueError("No cells found for parameterization.")
        with concurrent.futures.ProcessPoolExecutor(nc) as executor:
            pints = list(
                executor.map(self.read_parameterized_intensity, self.CellIds))
        pints = [p for p in pints if p is not None]
        agg_pint = self.agg_func(np.array(pints), axis=0)
        ch = self.control.get_aliases_to_parameterize().index(self.row.alias)
        self.aggregated_parameterized_intensity = agg_pint[ch].copy()
        return

    def set_agg_function(self):
        if self.row.aggtype == 'avg':
            self.agg_func = np.mean
        elif self.row.aggtype == 'std':
            self.agg_func = np.std
        else:
            raise ValueError(f"Aggregation type {self.row.aggtype} is not implemented.")

    def voxelize_and_parameterize_shapemode_shape(self):
        n = self.control.get_number_of_interpolating_points()
        alias_outer = self.control.get_outer_most_alias_to_parameterize()
        alias_inner = self.control.get_inner_most_alias_to_parameterize()
        mesh_outer = self.read_map_point_mesh(alias_outer)
        mesh_inner = self.read_map_point_mesh(alias_inner)
        domain, origin = cytoparam.voxelize_meshes([mesh_outer, mesh_inner])
        coords_param, _ = cytoparam.parameterize_image_coordinates(
            seg_mem=(domain>0).astype(np.uint8),
            seg_nuc=(domain>1).astype(np.uint8),
            lmax=self.control.get_lmax(), nisos=[n, n]
        )
        self.domain = domain
        self.origin = origin
        self.coords_param = coords_param
        return

    def morph_on_shapemode_shape(self):
        self.voxelize_and_parameterize_shapemode_shape()
        self.morphed = cytoparam.morph_representation_on_shape(
            img=self.domain,
            param_img_coords=self.coords_param,
            representation=self.aggregated_parameterized_intensity
        )
        self.morphed = np.stack([self.domain, self.morphed])
        return

if __name__ == "__main__":

    config = general.load_config_file()
    control = controller.Controller(config)

    parser = argparse.ArgumentParser(description='Batch aggregation.')
    parser.add_argument('--csv', help='Path to the dataframe.', required=True)
    args = vars(parser.parse_args())

    df = pd.read_csv(args['csv'], index_col=0)

    aggregator = Aggregator(control)
    for index, row in tqdm(df.iterrows(), total=len(df)):
        '''Concurrent processes inside. Do not use concurrent here.'''
        aggregator.execute(row)
