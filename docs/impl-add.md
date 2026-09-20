## Track Name and Flags for Remuxing

The Track Name for Audio:

The general audio track name should be "{Language} {DTS/DTS-MA/Dolby Digital/Dolby Atoms/or other format} {channel}". For example, "English DTS 5.1".

The Track Name for subtitles:

The general subtitle track name should be "{Language} {PGS/SRT/ASS/or other format} {SDH Flag/None} {Forced Flag/None}"

When necessary, LLM can be used.

## The title in the remuxing file

The title of the remuxing file (not file name) should be in format of "{Movie Name} ({Year})". All names of the movie should use the english version. When necessary, LLM can be used.

## Questions:

1. CRF Studio: https://github.com/gdjs2/BDRip_Scripts, There are other tools for BBCode generation and screenshots upload in this library. You can keep for later use. Sup2sup: https://github.com/gdjs2/Sup2Sup
2. I have 43631.src.png and 43631.wiki.png in docs, which are reference screenshots. The numbering is 0-based.
3. You can skip the HDR and interlanced formats. Just rejects these formats for now.
4. The screenshots should focus on characters and should indicate the quality of the encoding. You should have 7 each for x264 and x265 encoding. They cannot share the same frame / close frames.
5. Yes. For intermediate product you can use any name you want. But for the final product, you need to follow the name described in the BDRip_Scripts.
