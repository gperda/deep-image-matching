import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict

import h5py
import numpy as np
import pycolmap
from tqdm import tqdm

from deep_image_matching.utils import (
    COLMAPDatabase,
    compute_epipolar_errors,
    get_pairs_from_file,
)

logger = logging.getLogger("dim")


# Utils functions
def parse_retrieval(path):
    retrieval = defaultdict(list)
    with open(path, "r") as f:
        for p in f.read().rstrip("\n").split("\n"):
            if len(p) == 0:
                continue
            q, r = p.split()
            retrieval[q].append(r)
    return dict(retrieval)


def create_db_from_model(reconstruction: pycolmap.Reconstruction, database_path: Path) -> Dict[str, int]:
    if database_path.exists():
        logger.warning("The database already exists, deleting it.")
        database_path.unlink()

    with COLMAPDatabase.connect(database_path) as db:
        db.create_tables()

        for i, camera in reconstruction.cameras.items():
            db.add_camera(
                camera.model.value,
                camera.width,
                camera.height,
                camera.params,
                camera_id=i,
                prior_focal_length=True,
            )

        for i, image in reconstruction.images.items():
            db.add_image(image.name, image.camera_id, image_id=i)

        db.commit()

    return {image.name: i for i, image in reconstruction.images.items()}


def get_keypoints(features_h5: Path, name: str) -> np.ndarray:
    with h5py.File(str(features_h5), "r", libver="latest") as f:
        if name not in f:
            raise KeyError(f"Key '{name}' not found in '{features_h5}'")
        return f[name]["keypoints"][:]


def get_matches(matches_h5: Path, name0: str, name1) -> np.ndarray:
    with h5py.File(str(matches_h5), "r", libver="latest") as f:
        if name0 not in f:
            if name1 in f:
                name0, name1 = name1, name0
            else:
                raise KeyError(f"Key '{name0}' and '{name1}' not found in '{matches_h5}'")
        return f[name0][name1][:]


def import_keypoints(features_h5: Path, image_ids: Dict[str, int], database_path: Path) -> None:
    with COLMAPDatabase.connect(database_path) as db:
        for name, image_id in tqdm(image_ids.items(), desc="Importing keypoints"):
            keypoints = get_keypoints(features_h5, name)
            keypoints += 0.5  # COLMAP origin
            db.add_keypoints(image_id, keypoints)
        db.commit()


def import_matches(
    matches_h5: Path,
    image_ids: Dict[str, int],
    database_path: Path,
    pair_file: Path,
    add_two_view_geometry: bool = False,
):
    pairs = get_pairs_from_file(pair_file)
    with COLMAPDatabase.connect(database_path) as db:
        for name0, name1 in tqdm(pairs, desc="Importing matches"):
            matches = get_matches(matches_h5, name0=name0, name1=name1)
            id0, id1 = image_ids[name0], image_ids[name1]
            db.add_matches(id0, id1, matches)
            if add_two_view_geometry:
                db.add_two_view_geometry(id0, id1, matches)
        db.commit()


def import_verifed_matches(
    image_ids: Dict[str, int],
    reference: pycolmap.Reconstruction,
    database_path: Path,
    features_path: Path,
    matches_path: Path,
    pairs_path: Path,
    max_error: float = 4.0,
):
    logger.info("Performing geometric verification of the matches...")

    pairs = parse_retrieval(pairs_path)

    db = COLMAPDatabase.connect(database_path)

    inlier_ratios = []
    matched = set()
    for name0 in tqdm(pairs, desc="Importing verified matches"):
        id0 = image_ids[name0]
        image0 = reference.images[id0]
        cam0 = reference.cameras[image0.camera_id]
        kps0 = get_keypoints(features_path, name0)
        noise0 = 1.0
        if len(kps0) > 0:
            kps0 = np.stack(cam0.cam_from_img(kps0))
        else:
            kps0 = np.zeros((0, 2))

        for name1 in pairs[name0]:
            id1 = image_ids[name1]
            image1 = reference.images[id1]
            cam1 = reference.cameras[image1.camera_id]
            kps1 = get_keypoints(features_path, name1)
            noise1 = 1.0
            if len(kps1) > 0:
                kps1 = np.stack(cam1.cam_from_img(kps1))
            else:
                kps1 = np.zeros((0, 2))

            matches = get_matches(matches_path, name0, name1)

            if len({(id0, id1), (id1, id0)} & matched) > 0:
                continue
            matched |= {(id0, id1), (id1, id0)}

            if matches.shape[0] == 0:
                db.add_two_view_geometry(id0, id1, matches)
                continue

            cam1_from_cam0 = image1.cam_from_world * image0.cam_from_world.inverse()
            errors0, errors1 = compute_epipolar_errors(cam1_from_cam0, kps0[matches[:, 0]], kps1[matches[:, 1]])
            valid_matches = np.logical_and(
                errors0 <= cam0.cam_from_img_threshold(noise0 * max_error),
                errors1 <= cam1.cam_from_img_threshold(noise1 * max_error),
            )
            # TODO: We could also add E to the database, but we need
            # to reverse the transformations if id0 > id1 in utils/database.py.
            db.add_two_view_geometry(id0, id1, matches[valid_matches, :])
            inlier_ratios.append(np.mean(valid_matches))
    logger.info(
        "mean/med/min/max valid matches %.2f/%.2f/%.2f/%.2f%%.",
        np.mean(inlier_ratios) * 100,
        np.median(inlier_ratios) * 100,
        np.min(inlier_ratios) * 100,
        np.max(inlier_ratios) * 100,
    )

    db.commit()
    db.close()
