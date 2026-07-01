# BBBC038 Segmentation Benchmark Workflow

This demo workflow compares several nuclei segmentation methods on BBBC038-style nuclei images and evaluates each predicted label image against reference instance masks.

## Goal

Benchmark Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method on a shared nuclei segmentation task.

## Data

The workflow follows the BBBC038v1 `stage1_train` layout.
Each sample folder contains one raw microscopy image under `images/` and one or more per-object reference masks under `masks/`.

## How It Works

The workflow lists sample folders, combines the per-object mask files into one reference label image, prepares a 2D intensity image for segmentation, runs each method on that image, and writes one benchmark row per method and image.

## Results

- Predicted label images from Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method.
- Overlay previews for visual comparison.
- A benchmark table with object counts, foreground IoU, and Dice scores.

## Run

```bash
python example_workflows/bbbc038_segmentation_benchmark/workflow.py
```
