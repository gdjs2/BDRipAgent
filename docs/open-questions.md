# Remaining decisions after impl-add.md

The supplied tool repositories, zero-based frame numbering, SDR/progressive-only
scope, character emphasis and WiKi naming are implemented. The later screenshot
review request supersedes the seven-frame final count: the agent recommends the
best 15, all 40 shortlisted source frames are browsable, and the user chooses any
1–15 final pairs.
Audio/subtitle names and remux flags follow the added requirements. “Dolby Atoms”
is interpreted as the standard spelling **Dolby Atmos**; absent subtitle flags are
omitted rather than rendered as the word “None”.

Please refine these remaining assumptions:

1. **How close is too close?** The current minimum is 30 seconds within and across
   codec sets for the same immutable source. The default best 15 contain at least
   ten character frames, with nine representative / six encoding-challenge choices.
   Users may edit the best list from the shortlist and choose any subset.
   Constraints fail explicitly if a short film or long take cannot satisfy them;
   they are not silently relaxed. Confirm the gap and any desired exception policy.
2. **Release classes.** The upstream naming function fixes `1080p`,
   `BluRay`, `-WiKi` and x265 `10bit`. This implementation follows it for filenames;
   the MKV container title uses `Movie Name (Year)`. Should 720p/2160p SDR sources,
   editions/cuts or another group be supported? The worker currently preserves
   the source resolution.
3. **Encoder presets.** The existing slow profiles remain, now explicitly using
   high/main10. CRF Studio receives those same settings; its own placebo/slower
   presets and extra parameters are not silently substituted. Should the project
   adopt the upstream encoding presets, including different animation settings?
4. **Exact overlay font.** Both supplied PNGs were used to calibrate text geometry,
   baseline and spacing. Liberation Sans Bold is a close open substitute; glyph
   rasterization is not pixel-identical. Supply the original font/version if that
   degree of matching is required. Picture type remains each image's own decoded
   frame type, with the shared zero-based source frame number.
5. **Accounts and retention.** The earlier compound question received “Yes” without
   identifying an account model or cleanup policy. The implementation retains the
   single-operator token and confirmed soft-delete/file-retention behavior. Clarify
   only if multiple users/roles or automatic cleanup are required.

## Acceptance still needed

Both codec pipelines have run with real CRF Studio, HandBrake, MKVToolNix and
synthetic video. Sup2Sup has been checked with synthetic PGS cues and scaled crops,
and final names/flags were inspected using actual MKVToolNix output. Live Codex
selection still requires your container login and a representative film. Synthetic
fixtures do not establish feature-film visual quality or all real-world PGS/audio
edge cases. See `testing.md` for the exact coverage.

Existing completed jobs retain their original files. New jobs use the selected
IMDb title and year, or manually entered title/year when IMDb ID is empty. Filename,
MKV title and screenshot naming use those saved values. An old unfinished job
without a year should be recreated with IMDb lookup or manual title/year before
remuxing; source files are retained.
