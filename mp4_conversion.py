import subprocess
import sys
import os

def mp4_to_wav(input_file, output_file=None, sample_rate=44100, channels=2):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    if output_file is None:
        base = os.path.splitext(input_file)[0]
        output_file = base + ".wav"

    command = [
        "ffmpeg",
        "-i", input_file,
        "-vn",                 # no video
        "-acodec", "pcm_s16le",  # uncompressed PCM
        "-ar", str(sample_rate), # sample rate e.g. 44100
        "-ac", str(channels),    # channels: 1 (mono) or 2 (stereo)
        output_file,
        "-y"                    # overwrite without asking
    ]

    try:
        subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg failed with exit code {exc.returncode}: {exc.stderr.strip()}"
        ) from exc

    print(f"Converted: {output_file}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python converter.py input.mp4 [output.wav]")
    else:
        mp4_to_wav(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
