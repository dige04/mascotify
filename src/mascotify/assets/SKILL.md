---
name: mascotify
description: Create animated mascot assets for an app or game from a character description or reference image, and write them into the project. Use when the user wants a mascot, a character animation, a sprite sheet, an animated logo character, a loading character, or an empty-state character, or asks to animate an existing character image. Produces sprite sheets, animated WebP, Lottie, and platform bundles for iOS, Android, web, Unity and Godot. Do not use for UI micro-interactions, icon animation, or anything better done in CSS or code.
---

# Mascotify

You generate the images. `mascotify` compiles the prompts and processes what you
produce. No API key is involved — your own image generation tool is the provider.

## The loop

```
mascotify anchor "<character>"   → prompt for one canonical still
        ↓  you generate it, save to ref.png
mascotify plan --ref ref.png --action wave   → prompt for the 12-frame grid
        ↓  you generate it, save to the path plan printed
mascotify ingest <that path>     → validates, then exports every platform
        ↓  if it fails, it prints a repair prompt — regenerate and ingest again
```

Never hand-edit a generated image. Regenerate instead: the validator measures
things that hand-editing cannot fix, like character scale drift between cells.

## Step 1 — the anchor

Skip only if the user already has character art with a flat background.

```bash
mascotify anchor "a friendly rounded robot, big single visor eye, teal and cream"
```

It prints a prompt and a target path. Generate that image with your image tool,
save it to `ref.png`, then **look at it** before continuing. The anchor is the
identity source for every animation that follows; a flaw here propagates to all
of them. Regenerate if the character is cropped, sitting on a shadow, or not
what the user described.

## Step 2 — plan the animation

```bash
mascotify plan --ref ref.png --action wave
```

Pick `--action` from `mascotify motions`: `wave`, `idle`, `bounce`, `nod`,
`blink`, `point`, `typing`, `note-taking`, `think`, `celebrate`. Any other
string works too — pass a description with `--describe`.

Choosing well matters more than it looks. These survive being drawn twelve times
because the character stays planted and the camera never moves. Anything
involving walking, turning, or a change of viewing angle will drift badly. If
the user asks for one, say so and offer the nearest stationary motion.

Useful flags:

| flag | when |
| --- | --- |
| `--rows 4 --cols 6 --fps 24` | 24 frames, much smoother, same zero cost |
| `--fps 8` | slower, sleepier idle |
| `--base-pt 96` | smaller render box on iOS |
| `--targets web,ios` | skip platforms the project does not use |

## Step 3 — generate the sheet

`plan` prints the exact prompt and the exact path. Load `ref.png` into context
first (`view_image`, or whatever your tool calls it) so the character is visible
to you, then generate one image and save it to that path.

The requested size is a hint. Mascotify measures the real grid, so a generator
that emits a different resolution is fine.

## Step 4 — ingest

```bash
mascotify ingest <path plan printed>
```

Exit 0 means it exported. Exit 2 means validation failed and it printed a repair
prompt — regenerate with that prompt and ingest again. Two rerolls is normal.
Three means the motion is fighting the format; change the motion or the grid.

What it checks, and why each one exists:

- **frame count** — a merged or split cell means the grid was not read as asked
- **canvas clipping** — the single most common failure. Models fill the canvas
  edge to edge and amputate the bottom row
- **scale spread** — the character must not change size between cells
- **loop seam** — decides on its own whether the loop needs ping-pong

`mascotify doctor <sheet.png>` runs the same checks as JSON without exporting.

## Also look at it yourself

The validator measures geometry. It cannot see a broken hand, a wrong number of
fingers, an eye that drifts, or a wave that reads as a salute. You can. Read the
generated sheet before you ingest it, and reroll on anatomy the same way the
tool rerolls on geometry. This is the main advantage you have over a web app.

## Step 5 — put the assets in the project

Exports land in `.mascotify/<job>/export/<platform>/`. Copy what the project
actually uses into the project's own asset directory, and wire up the snippet
`ingest` printed. Match the project's conventions — look at where its existing
images live rather than assuming.

| project marker | export | goes to |
| --- | --- | --- |
| `*.xcodeproj`, `*.swift` | `ios/` | drag `Mascot.xcassets` in, add `MascotSprite.swift` |
| `build.gradle`, `AndroidManifest.xml` | `android/` | merge `res/drawable/` |
| `package.json` | `web/` | `public/` or `assets/`, import the CSS |
| `Assets/`, `*.unity` | `unity/` | slice at the cell size it reports |
| `project.godot` | `godot/` | load the `.tres` |
| lottie already in use | `lottie/` | drop in the JSON |

Then delete the rest — do not commit six platform bundles when the project ships
one.

## The video tier costs money

`mascotify video` uses an image-to-video model and needs `FAL_KEY` or
`REPLICATE_API_TOKEN`. Your image tool cannot do this one.

Never run it without telling the user it will charge their key, roughly $0.25 a
clip. It refuses without `--yes` for that reason; do not add `--yes` on the
user's behalf unless they already agreed to the spend in this conversation.

Before reaching for it, offer the free alternative: `--rows 4 --cols 6 --fps 24`
gives 24 frames in one image at no cost, which covers most of what people want
video for.

## Keeping one mascot consistent across animations

Frames within a single sheet come out near-identical to each other. Separate
sheets drift from the anchor — and from each other. For several animations of
one mascot, reuse the same `ref.png` for every `plan`, then check each result:

```bash
mascotify compare --ref ref.png --against .mascotify/wave/sheet.png --out compare.png
```

It writes a strip with the anchor beside sampled frames, all scaled to the same
height. **Read that strip.** Scale-matching is the whole point — a 1024px anchor
next to a 390px sheet frame is not a comparison anyone can judge, including you.

Look for the small things, because that is where drift lives: limb thickness,
how long an antenna or ear is, the size of a chest or belly marking, head
roundness. The overall silhouette and palette usually survive; the details do
not.

`compare` prints palette and aspect numbers as context. **Do not treat them as a
verdict** — measured against real drift they contradict each other, and neither
tracks what a person perceives as "the same character". They are there to tell
you something moved, not whether it matters.

If a sheet has drifted, reroll it against the anchor rather than accepting a
mascot that changes between screens. If several rerolls keep drifting the same
way, the anchor is probably ambiguous about that detail — regenerate the anchor
with it stated explicitly, and start over from there.
