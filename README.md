# Skeleton Pipeline

Input: reconstructed `.ims` file by Luxendo.

| Step | Script | Input | Output | What it does |
|---|---|---|---|---|
| 1 | `01_convert.py` | `.ims` | one 8-bit `.h5` per channel, `-meta.json` | Converts every channel to uint8, chunked, gzip-compressed `.h5` for ilastik |
| 2 | `02_generate_crops.py` | `.h5` (step 1), `.ims` | crops (`.h5`), `meta.json`, overview `.png` | Cuts random crops from the tissue region, for training ilastik |
| 3 | manual (ilastik GUI) | crops (step 2) | trained `.ilp` | Train the pixel classifier: label foreground / background |
| 4 | `03_run_ilastik_headless.py` | `.h5` (step 1), trained `.ilp` (step 3) | probability `.h5` (uint8, foreground only) | Runs the trained pixel classifier headless on the full volume |
| 5 | `02_generate_crops.py` | probability `.h5` (step 4), `.ims` | crops (`.tif`), `meta.json`, overview `.png` | Cuts random crops from the probability map, for viewing |
| 6 | manual (Imaris) | crops (step 5) | threshold 0–255 | View the crops and decide on the threshold |
| 7 | `04_apply_threshold.py` | probability `.h5` (step 4), threshold (step 6) | binary mask `.h5` (0/1), `-meta.json` | Thresholds the probabilities into a foreground mask |


```mermaid
flowchart TD
    ims[/"Luxendo .ims"/]

    s1["1 · 01_convert.py"]
    h5[/".h5 per channel (uint8)"/]

    s2["2 · 02_generate_crops.py"]
    crops_train[/"crops .h5"/]

    s3{{"3 · manual: train in ilastik GUI"}}
    ilp[/"trained .ilp"/]

    s4["4 · 03_run_ilastik_headless.py"]
    prob[/"probability .h5 (foreground, 0–255)"/]

    s5["5 · 02_generate_crops.py"]
    crops_view[/"crops .tif"/]

    s6{{"6 · manual: view in Imaris"}}
    thr[/"threshold 0–255"/]

    s7["7 · 04_apply_threshold.py"]
    mask[/"binary mask .h5 (0/1)"/]

    ims --> s1 --> h5
    h5 --> s2
    ims -. tissue region .-> s2
    s2 --> crops_train --> s3 --> ilp
    h5 --> s4
    ilp --> s4
    s4 --> prob
    prob --> s5
    ims -. tissue region .-> s5
    s5 --> crops_view --> s6 --> thr
    prob --> s7
    thr --> s7
    s7 --> mask

    classDef script fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef manual fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef data fill:#f3f4f6,stroke:#6b7280,color:#111827
    class s1,s2,s4,s5,s7 script
    class s3,s6 manual
    class ims,h5,crops_train,ilp,prob,crops_view,thr,mask data
```
