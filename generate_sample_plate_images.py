"""Generate sample ANPR plate PNGs for Phase-2 image-upload demo."""

from plate_ocr import SAMPLE_DIR, generate_samples_from_manifest

if __name__ == "__main__":
    paths = generate_samples_from_manifest(30)
    print(f"Wrote {len(paths)} images under {SAMPLE_DIR}")
    for p in paths[:8]:
        print(" ", p.name)
