# Test data

* `dialog.ogg`: 22 s, 16 kHz mono Opus. Two synthetic voices (espeak-ng `en-us` and `en-us+f3`) take turns,
  separated by 1.5 s of silence:
  1. "Good afternoon everyone. My name is Peter and I am the father of the bride."
  2. "Thank you Peter. I just want to say how happy we are to be here today."
  3. "Please raise your glasses for Anna and David."
  4. "Cheers to the happy couple."

  Made with `espeak-ng -w` for each line, concatenated with FFmpeg's `concat` filter, and encoded with
  `-c:a libopus -b:a 24k`.
