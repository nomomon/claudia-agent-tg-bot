# Voice reply segments

Experiment on the production container, 2026-10-26, Gemini `gemini-2.5-pro-preview-tts`, voice Achernar, one API key. Each length was run twice unless noted.

## Results

| Text length | Latency | Audio length |
|---|---|---|
| 200 chars | 15.4s, 15.6s, 15.8s, 14.5s | 15.9s, 18.2s, 17.2s, 16.3s |
| 450 chars | 23.8s, 27.9s | 27.2s, 31.6s |
| 900 chars | no reply within 240s (twice) | n/a |
| 3 parallel, 200 chars | 15.2s to 16.2s each, 16.2s wall | 15.5s to 18.4s |
| 2 parallel, 450 chars | no reply within 240s | n/a |

## What it means

- Latency is about the same as the audio length. A 200-character segment is ready in about 15 seconds.
- Long single requests did not come back. Earlier runs of the same text were also slow. The 900-character request has no timeout in the old code, so a hung request would block a reply forever.
- Three short requests in parallel finished in the time of one. Parallel synthesis works at this size.
- The two parallel 450-character requests also hung. I did not find the cause, so segments stay short.

## Decision

- First segment 200 characters, about 15 seconds of speech. The first voice note arrives in about 15 seconds.
- Later segments up to 250 characters.
- Three segments synthesized in parallel. Each takes about 15 seconds to make, and each plays for about 15 to 18 seconds, so generation stays ahead of playback.
- Each request has a 60-second timeout. A timeout counts as a transient error and is retried.

## Limits

- Two runs per length, one key, one day. Latency will vary.
- A single sentence longer than the segment size is not split, so it can still be a long request.
