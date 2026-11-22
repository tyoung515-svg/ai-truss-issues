# AI Truss Issues setup and recording guide

Run local or Codex language models against the Windows Steam version of Poly Bridge 2. The harness gives each model a 4 m and an 8 m bridge to build, then tests the submitted design in the game. By default, the model gets five tries per gap. Once a design gets the car across with zero breaks, the remaining tries aim to lower the cost.

## 1. Set up the game host

You’ll need a 64-bit Windows PC, an installed copy of Poly Bridge 2 from Steam, Python 3.10 or newer, and the benchmark source folder. The source folder should contain `setup.ps1`, `bench.py`, `providers.py`, `blueprint.py`, and both C# files in `game-host`. [Python for Windows](https://www.python.org/downloads/windows/)

Close Poly Bridge 2 and open PowerShell in the benchmark source folder. Setup finds the Steam installation, makes a separate game copy under `%LOCALAPPDATA%\PolyBridgeBenchmark`, downloads and verifies BepInEx, and builds the benchmark host. Each run uses a fresh save profile.

```powershell
python --version
python -m pip install "Pillow>=10.1"
.\setup.ps1
python .\bench.py run --player reference
```

The reference player runs one deterministic design per gap and requires no model. On Poly Bridge 2 v1.73, expect a zero-break crossing at **$2,458** for 4 m and **$5,276** for 8 m. Treat this as a check that the game host is working.

If setup can’t find the game, use the installed folder containing `Poly Bridge 2.exe`. In Steam, open the game’s **Manage → Browse local files** to find it. Replace the example path with yours.

```powershell
.\setup.ps1 -GameSource '<installed game folder>'
```

## 2. Try a model with Ollama

Install and launch Ollama for Windows. This example uses a small text-only model to check the connection and run one 4 m attempt. The tag must appear in both `ollama list` and `models.json`. The registered Qwen3 4B context is 131,072 tokens. [Ollama for Windows](https://docs.ollama.com/windows) · [Qwen3 4B](https://ollama.com/library/qwen3:4b)

```powershell
ollama pull qwen3:4b
ollama list
python .\bench.py run --player ollama:qwen3:4b --case gap4 --attempts 1
```

A placement rejection still means the harness received a design and the game checked it. Omit `--case gap4 --attempts 1` to run both gaps with five tries each.

## 3. Give a vision model the blueprint

This example uses Gemma 4 12B IT `UD-Q5_K_XL` with its matching `mmproj-F16.gguf` projector. The configuration was tested with llama.cpp b12590 on a 16 GB NVIDIA GPU. Download both GGUF files from the pinned model release. Extract the Windows CUDA 12.4 binary and its matching CUDA runtime archive to the same directory. [Gemma weights and projector](https://huggingface.co/unsloth/gemma-4-12b-it-GGUF/tree/fc034cfff751157913579611efad8462ac1be606) · [llama.cpp b12590](https://github.com/ggml-org/llama.cpp/releases/tag/b12590)

Use the resolver to check the files, then launch a blueprint try. Pass `--llama-server PATH` if the executable is outside the registered tools directory.

```powershell
python .\tools\score.py --model gemma --resolve-only
python .\tools\score.py --player gemma --blueprint --case gap4 --attempts 1
```

`models.json` pins the repository, revision, filenames, runtime build and model settings. `tools/score.py --resolve-only` shows the files it finds in your Hugging Face cache and the `hf download` commands for missing files. For loose files, configure `local_path` and `projector_local_path` under the model key in an ignored `models.local.json` beside the registry. Use `--llama-server PATH` or `PB2_LLAMA_SERVER` for a custom executable path. The `--model` probe mode starts the isolated game, tries contexts from 128k downward and records VRAM and a probe per loaded model. Add `--smoke --blueprint` for a short scoring check. `--save-contexts` writes the successful size into the registry. Full comparisons use the registered contexts.

Run a comparison with `python .\tools\score.py --player e4b --player gemma --attempts 1 --case gap4` or preview the full lineup with `--plan v6-full --dry-plan`. The full plan enables blueprints and includes every ready llama.cpp model plus Astra at `@low` and `@xhigh`. Flash Next and Qwen 3.8 27B also run at both levels. To continue after a stop, use `python .\tools\score.py --resume RUN_DIR`.
Close the Steam game before benchmarking. The launcher saves and restores its shared Unity registry settings and recovers interrupted restores on the next launch.

Every image-enabled attempt receives the empty gap. A parseable design gets a free placement-review turn before simulation, with a labeled proposed-design image and host validation. The model confirms or revises; malformed review falls back to its original design. Later attempts also receive separate stress-over-time frames, showing actual deformation, car position and instantaneous stress. All frames use the same meter grid. Images work with llama.cpp, Codex `-i` and vision-capable Ollama models; text-only Ollama models reject `--blueprint` cleanly.

Local preflight counts a synthetic last-attempt history at 48 joints and 96 members plus review and up to nine images. The prompt plus 32,768 output tokens must fit the live context. llama.cpp can spend up to 16,384 of those output tokens on reasoning. Arbitrarily verbose JSON is outside the synthetic bound, and the harness also checks each actual local request. Codex remains eligible with preflight recorded as unavailable.

Use `--repeats 3 --seed 101` with `bench.py run` to repeat a player/case with independent history and distinct recorded seeds. Codex has no seed flag. The direct llama.cpp command below requires an already-running server with the registered alias. The Codex command requires an installed and signed-in Codex CLI.

```powershell
python .\bench.py run --player llama:gemma --llama-port 18088 --case gap4 --attempts 2 --blueprint --repeats 3
python .\tools\score.py --model flash@low --smoke --case gap4 --attempts 1 --blueprint
python .\bench.py run --player codex:gpt-6.2-astra@low --player codex:gpt-6.2-astra@xhigh --case gap4 --attempts 1 --blueprint
python -m unittest -v
```

## Optional: Strata runtime for Flash Next

Start with the llama.cpp `flash` player above. It needs no extra software. The v6 dry run also ran Flash Next on [Strata](https://github.com/Niko1221/Strata) (MIT License), an engine for this model that keeps its experts in system RAM. Strata needs more setup:

- an NVIDIA card with driver 580 or newer, because the harness uses Strata's CUDA 13 engine;
- about 64 GB of RAM;
- about 10 GB of extra disk for the prepared pack and the draft layer.

The player is `strata:flash-strata@low` or `strata:flash-strata@high`. Strata's top thinking level is `high`. It uses the same weights as `llama:flash`, but the engine is different, so compare its scores with that in mind.

1. Download the pinned Flash Next GGUF and projector, the same files the llama.cpp player uses. The resolver prints the `hf download` commands for anything missing.

   ```powershell
   python .\tools\score.py --model flash --resolve-only
   ```

2. Install the Strata Terminal from release 0.2.11. That release ships the 0.1.57 Terminal and engine the harness pins. To read the installer first, save it with `irm "$env:STRATA_FEED/install.ps1" -OutFile install.ps1` instead of piping it.

   ```powershell
   $env:STRATA_FEED = 'https://github.com/Niko1221/Strata/releases/download/v0.2.11'
   irm "$env:STRATA_FEED/install.ps1" | iex
   ```

3. Prepare the pack, the draft layer and the engine once with Strata's own launcher. Point it at the Hugging Face snapshot and at the data folder the harness reads. Run it with `--plan` first to see what it will download. Then run it again without `--plan`. Once it is serving, stop it with `strata launch --stop`: the harness starts its own Strata server with a run-local config.

   ```powershell
   $snapshot = "$HOME\.cache\huggingface\hub\models--unsloth--Qwen3.8-Flash-Next-GGUF\snapshots\d96201194f103c906994f283f504209dc8d2a10c\UD-IQ2_XS"
   strata launch --family unsloth --model UD-IQ2_XS --model-path $snapshot --data-dir "$HOME\Strata-data" --drafting mtp --feed $env:STRATA_FEED --plan
   ```

4. Check the files, then run a one-try smoke test.

   ```powershell
   python .\tools\score.py --model flash-strata --resolve-only
   python .\tools\score.py --model flash-strata@low --smoke --case gap4 --attempts 1 --blueprint
   ```

If your Hugging Face cache isn't in the default place, change `$snapshot`. Set `PB2_STRATA_DATA` if you keep the Strata data folder somewhere other than `$HOME\Strata-data`, and `STRATA_DATA_DIR` if you installed Strata outside `%LOCALAPPDATA%\Strata`. The harness runs Strata with a 131,072-token context, MTP drafting, a 2,000 MiB VRAM reserve and a CPU picture reader. It records the launch command, engine build and shutdown next to each run. The full `--plan v6-full` lineup leaves Strata out. Add it with `--player strata:flash-strata@low`.

These steps match the setup on the machine that ran the dry run. Nobody has run them yet on a clean PC.

## Recording video

Add `--watch` to open **Poly Bridge 2 Benchmark** at 1920×1080 with game sound and stress colors. OBS can capture this named window at 1080p30 with a Window Capture source. In the printed run directory, add a Text source with **Read from file** for `live/caption.txt` and an Image source for `live/model-view.png`. Add another Text source for `live/scoreboard.txt` if you want the running score on screen. The result stays on screen eight seconds. Use `--watch-hold SECONDS` to change that, `--watch-mute` to mute the game, and `--watch-size WxH` to resize it.

Use `replay` to record a highlight from an existing run without calling a model.

```powershell
python .\bench.py run --player reference --watch
python .\bench.py replay RUN_DIR --case gap8 --attempt 3 --player "Gemma 4 12B IT" --watch
```

## Read and share your results

Each command prints its run directory under `runs\`. Start with `summary.json` and look for `clean_passes`, `first_clean_pass_attempt`, and `best_pass_attempt`. A clean pass reaches the flag with zero broken members. The benchmark keeps the cheapest verified clean design even if a later revision fails.

- `manifest.json` records the game build, prompt version, model and attempt settings.
- `attempts.jsonl` records scored attempts and separate free review rows, prompts, replies, validation, image lists, settings sent, usage, finish reasons and timing. `summary.json` excludes reviews and groups repeats.
- `game\<id>.trace.json` records a 0.05-second sample schedule plus break moments: car position, physics joint positions, member stress and the game's signed stress. Peak fields remain in the result; compact trace summaries go into later prompts, while full traces stay on disk.
- The PNG files are the images sent to the model. `game\unity.log` is the place to start if the game host fails to boot.

To compare or share results, include the summary, exact model tag or GGUF quant, runtime, context size, and whether you used blueprints or thinking. Keep the cases, attempt budget, and input mode consistent across models.

## If a run gets stuck

| What you see | What to check |
| --- | --- |
| Game host never gets ready | Read `game\unity.log` from the printed run directory. Check that the game is closed before the benchmark starts, then rerun `.\setup.ps1` if the installation changed. |
| Model unavailable or connection refused | Check `ollama list` or query the llama.cpp server's `/v1/models` endpoint. Match the registered model tag or server alias and port exactly. |
| The server runs out of memory | Use a smaller GGUF or lower `-ngl` to place fewer layers on the GPU. |
| Preflight refuses the context | Check the measured prompt/image/output totals in `manifest.json`. Use a larger context that passes the launcher load/probe, or a smaller attempt budget. |

Press **Ctrl+C** in the benchmark window to stop a run. If you launched llama.cpp separately, stop it in its own window when you’re done.

