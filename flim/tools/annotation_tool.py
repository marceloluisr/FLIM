import argparse
import warnings
from contextlib import contextmanager
from os import path

import napari
import numpy as np
from magicgui import magicgui
from magicgui.widgets import FloatSlider
from numba import jit
from PIL import Image
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication
from skimage.segmentation import find_boundaries

try:
    import pyift.pyift as ift
except ModuleNotFoundError:
    ift = None
    warnings.warn("PyIFT is not installed.", ImportWarning)

import math

import torch


def get_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="Image to segment.")

    parser.add_argument(
        "-m", "--markers", help="Image markers for the the segmentation.", default=None
    )

    parser.add_argument("--mask", help="Image label mask.", default=None)

    parser.add_argument(
        "-ns", "--n-superpixels", help="The number of superpixels", type=int, default=0
    )

    args = parser.parse_args()

    return args


def load_label_image(label_path):
    if label_path.endswith(".txt"):
        with open(label_path, "r") as f:
            lines = f.readlines()
        label_infos = [int(info) for info in lines[0].split(" ")]

        # images dimensions are flipped
        image_shape = (label_infos[2], label_infos[1])
        label_image = np.zeros(image_shape, dtype=np.int32)

        for line in lines[1:]:
            split_line = line.split(" ")
            y, x, label = int(split_line[0]), int(split_line[1]), int(split_line[3])
            label_image[x][y] = label

        assert (label_image != 0).sum() == label_infos[
            0
        ], "There are zero markers. Be careful!!"
    else:
        label_image = np.array(Image.open(label_path))

    return label_image


def load_image(image_dir):
    image = np.array(Image.open(image_dir))

    return image


def _save_markers(markers, markers_dir):
    markers = markers.astype(np.int32)
    mask = markers != 0

    number_of_markers = mask.sum()
    markers_shape = markers.shape

    x_coords, y_coords = np.where(mask)

    with open(markers_dir, "w") as f:
        f.write(f"{number_of_markers} {markers_shape[1]} {markers_shape[0]}\n")
        for x, y in zip(x_coords, y_coords):
            f.write(f"{y} {x} {-1} {markers[x][y]}\n")


def image_to_ift_mimage(image):
    assert isinstance(
        image, (np.ndarray, torch.Tensor)
    ), "image must me a numpy array or a torch Tensor"

    if isinstance(image, torch.Tensor):
        image = image.numpy()

    assert image.ndim == 3, "image must have shape (H, W, C)"

    image = np.ascontiguousarray(image)

    image = ift.CreateMImageFromNumPy(image.astype(np.float32))

    return image


def get_superpixels_of_image(image, n_superpixels):
    assert isinstance(
        image, (np.ndarray, torch.Tensor)
    ), "image must me a numpy array or a torch Tensor"
    assert n_superpixels > 0, "number of superpixels must be positive"

    ift_mimage = image_to_ift_mimage(image)

    adj = ift.Circular(1.0)

    mask = ift.SelectImageDomain(ift_mimage.xsize, ift_mimage.ysize, ift_mimage.zsize)

    igraph = ift.ImplicitIGraph(ift_mimage, mask, adj)
    seeds = ift.GridSampling(ift_mimage, mask, n_superpixels)

    ift.IGraphISF_Root(igraph, seeds, 0.5, 12, niters=1000)

    labels = ift.IGraphLabel(igraph)

    roots = ift.IGraphRoot(igraph)

    return labels.AsNumPy(), roots.AsNumPy()


def get_superpixels_centers(superpixels):
    center = np.full((superpixels.max() + 1, 2), 0)

    for i in range(1, superpixels.max() + 1):
        mask = superpixels == i
        center[i] = np.round(np.argwhere(mask).mean(axis=0))

    return center


def get_superpixels_roots(superpixels, root_image):
    indices = np.arange(0, root_image.shape[0] * root_image.shape[1]).reshape(
        root_image.shape
    )

    roots = np.argwhere(root_image == indices)

    return roots


def get_markers_from_superpixels(image):
    superpixels, root_image = get_superpixels_of_image(image, n_superpixels=500)
    centers = get_superpixels_roots(superpixels, root_image)
    image_markers = np.zeros((image.shape[0:2]))
    image_markers[centers[:, 0], centers[:, 1]] = 1

    return image_markers.astype(np.int32)


@jit
def turn_superpixels_in_markers(superpixels, markers):
    new_markers = np.zeros_like(markers)

    labels = np.unique(superpixels)

    markers_mask = markers != 0

    for label in labels:
        superpixel_mask = superpixels == label
        # flag = np.any(np.logical_and(markers_mask, superpixel_mask))
        marker_label = markers_mask[superpixel_mask].max()

        new_markers[superpixel_mask] = marker_label

    return new_markers


@jit
def turn_superpixels_borders_in_markers(superpixels, markers):
    new_markers = np.zeros_like(markers)

    labels = np.unique(superpixels)

    markers_mask = markers != 0
    boundaries = find_boundaries(superpixels, connectivity=2, mode="inner").astype(
        np.int32
    )
    for label in labels:
        superpixel_mask = np.logical_and(superpixels == label, boundaries)
        # flag = np.any(np.logical_and(markers_mask, superpixel_mask))
        marker_label = markers_mask[superpixel_mask].max()

        new_markers[superpixel_mask] = marker_label

    return new_markers


@contextmanager
def wait_cursor():
    try:
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        yield
    finally:
        QApplication.restoreOverrideCursor()


def create_viewer(
    image_dir,
    markers_dir=None,
    n_superpixels=0,
    mask_dir=None,
):

    image = load_image(image_dir)
    initial = np.zeros(image.shape[:2], dtype=np.int32)

    if n_superpixels > 0:
        super_pixels, _ = get_superpixels_of_image(image, n_superpixels)
    else:
        super_pixels = None

    if markers_dir is not None:
        markers = load_label_image(markers_dir)
    elif path.exists(image_dir.split(".")[0] + ".txt"):
        markers = load_label_image(image_dir.split(".")[0] + ".txt")
    else:
        markers = initial

    if mask_dir is not None:
        mask = load_label_image(mask_dir)
    else:
        mask = None

    if super_pixels is not None and markers.max() > 0:
        markers = turn_superpixels_in_markers(super_pixels, markers)

    else:
        super_pixels = initial

    viewer = napari.Viewer(title="Interative tool.")
    viewer.add_image(image, name="image")

    if super_pixels is not None:
        boundaries = find_boundaries(super_pixels, connectivity=2, mode="inner").astype(
            np.int32
        )
        boundaries[boundaries != 0] = 9
        viewer.add_labels(boundaries, name="superpixels", opacity=1)
    else:
        viewer.add_labels(initial, name="superpixels", opacity=1)

    if mask is not None:
        viewer.add_labels(mask, name="mask", opacity=0.5)

    viewer.add_labels(markers, name="markers", opacity=1)

    @viewer.bind_key("r")
    def refresh(viewer):
        initial = np.zeros(image.shape[:2], dtype=np.int32)
        viewer.layers["markers"].data = initial
        # viewer.layers['instability map'].data = initial

    @magicgui(call_button="Save markers")
    def save_markers():
        print("Saving markers...")
        nonlocal markers_dir
        markers = viewer.layers["markers"].data

        if markers_dir is None:
            markers_dir = image_dir.split(".")[0] + ".txt"
        with wait_cursor():
            _save_markers(markers, markers_dir)
        print("Markers saved.")

    if ift is not None:

        @magicgui(call_button="Propagate markers")
        def propagate_markers():
            markers = viewer.layers["markers"].data
            new_markers = turn_superpixels_borders_in_markers(super_pixels, markers)
            viewer.layers["markers"].data = new_markers

        @magicgui(
            call_button="Compute superpixels",
            n_superpixels={"widget_type": FloatSlider, "max": 5000},
        )
        def compute_superpixels(n_superpixels=0):
            nonlocal super_pixels
            n_superpixels = math.floor(n_superpixels)
            with wait_cursor():
                if n_superpixels > 0:
                    print(n_superpixels)
                    super_pixels, _ = get_superpixels_of_image(image, n_superpixels)
                    boundaries = find_boundaries(
                        super_pixels, connectivity=1, mode="inner"
                    ).astype(np.int32)
                    boundaries[boundaries != 0] = 9
                    print(boundaries.max())
                    viewer.layers["superpixels"].data = boundaries

        compute_superpixels_button = compute_superpixels
        viewer.window.add_dock_widget(compute_superpixels_button, area="bottom")

        propagate_markers_button = propagate_markers
        viewer.window.add_dock_widget(propagate_markers_button, area="bottom")

    save_markers_button = save_markers
    viewer.window.add_dock_widget(save_markers_button, area="bottom")
    napari.run()


def main():
    args = get_arguments()
    create_viewer(args.image, args.markers, args.n_superpixels, args.mask)


if __name__ == "__main__":
    main()
