# BBBC038 Segmentation Benchmark Workflow

This workflow compares several nuclei segmentation methods on the same BBBC038-style inputs and evaluates each predicted label image against the sample's reference instance masks.

## Goal

Benchmark Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold branch on a shared nuclei segmentation task.

## Data

Use a selected BBBC038 `stage1_train` subset with the original `images/` and `masks/` folder layout.
Each sample folder should contain one raw microscopy image under `images/` and one or more per-object reference masks under `masks/`.

## How It Works

The workflow lists sample folders, combines the per-object mask files into one reference label image, prepares a 2D intensity image for segmentation, runs each method in its own branch, and writes one benchmark row per method and image.

## Results

- Predicted label images from Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method.
- Overlay previews for visual comparison.
- A benchmark table with object counts, foreground IoU, and Dice scores.

## Run

```bash
python example-workflows/bbbc038_segmentation_benchmark/workflow.py --data-dir data/bbbc038_stage1_train_subset
```
