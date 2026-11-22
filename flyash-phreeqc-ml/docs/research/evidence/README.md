# Check the published six-metal recovery calculation

This offline check reconstructs the combined recovery of scandium, lanthanum, cerium, praseodymium, neodymium and samarium for the Indian red mud studied by Aditya Ranjan Ray, Bankim Chandra Tripathy and Sujata Mishra. The authors report the feed digest in Table 1 and measurements from 20 citric-acid leaching runs in Table 4. The included CSV contains a single worked example from run 18. [Original article and tables, Scientific Reports (2025)](https://doi.org/10.1038/s41598-025-17134-7)

## Try one published row

From this directory, run the following command with Python 3.

```sh
python3 verify_ray_recovery.py --subset ray_example.csv
```

Expected output

```text
Run 18: calculated 90.1643% | published 90.16%
Checked 1 of 20 runs. Maximum difference: 0.004263 percentage points.
```

## Reproduce all 20

Create a UTF-8 CSV using the header in [ray_example.csv](ray_example.csv). Enter one row for each of runs 1 through 20 in Table 4.

- `run` is the published experiment number. Retain the separate IDs of repeat runs.
- `Sc`, `La`, `Ce`, `Pr`, `Nd` and `Sm` are the six dissolved-element concentrations as printed, in mg/L.
- `total_mg_L` is the printed sum of these concentrations, in mg/L.
- `published_total_pct` is the combined recovery printed in the paper, as a percentage number such as `90.16`. Keep printed zeros for this numerical reconstruction. A missing reading cannot be entered as a zero.

Run the full check with your CSV filename. It additionally checks a SHA-256 fingerprint of the 20 transcribed numerical rows, sorted by run ID. A different table can give internally consistent arithmetic and still fail this source check. The subset option checks the arithmetic of the rows you supply.

```sh
python3 verify_ray_recovery.py ray_table4.csv
```

The program confirms that every run appears exactly once and that each row's element concentrations sum to the reported total. It uses the sum of the six Table 1 digest concentrations, **24.960 mg/L**, as the denominator. Under the study's equal-volume convention the combined recovery percentage is `100 × summed leach concentration / summed digest concentration`. For run 18 the numerator is **22.505 mg/L**. With the published numbers all 20 differences are below 0.005 percentage points, half the last reported decimal place. The maximum is **0.004712 percentage points**. An incomplete full table, missing numeric value or failed comparison gives a nonzero exit status.

This result verifies arithmetic using measured concentrations from one publication. An experimental mass recovery for a new run needs the dry feed assay, actual liquid volumes, dilutions, withdrawn samples and relevant blanks. Accuracy in predicting release from a new material requires a separate experiment. The source's printed zeros do not establish an analytical detection limit.
