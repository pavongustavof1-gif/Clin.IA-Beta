# Text-only scenarios

Invented transcripts only, never real patients. One utterance per line:

    [Hablante A]: Buenos días, ¿qué le trae por aquí?
    [Hablante B]: Me duele la garganta desde ayer.

A scenario YAML points here with `transcript: transcripts/BO-04.txt` (relative to
`bakeoff/inputs/`) instead of `audio:`. Lines are parsed into the same
`[{"speaker": "A", "text": ...}]` utterances the audio scenarios produce. A line
that doesn't start with `[Hablante X]:` continues the previous utterance.
