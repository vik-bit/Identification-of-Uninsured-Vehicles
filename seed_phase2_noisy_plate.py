"""
seed_phase2_noisy_plate.py
--------------------------
Thin wrapper — Phase-2 now bulk-loads noisy data via populate_noisy_data.py
(includes the classic DL9CAF8888 / DL9CAF8B88 demo pair).
"""

from populate_noisy_data import populate


if __name__ == "__main__":
    populate(pairs=40)
