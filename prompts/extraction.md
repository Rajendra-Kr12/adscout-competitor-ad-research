# Per-ad extraction (prompt_version: extract-v1)

You get a batch of ads from one advertiser (`data/llm/<target>/<run>/input-NN.json`).
Each ad has `library_id`, `text` (`bodies`, `link_titles`, `link_descriptions`,
`link_captions`), a few deterministic `derived` metrics, dates and platforms.
You see **text only**. There are no images or video yet (Phase 1).

For every ad, return one `llm` object. It is validated against
`schemas/ad.schema.json#/$defs/llm`, and any ad that fails validation is rejected.

## Fields

| Field | What to write |
|---|---|
| `hook` | The opening line or claim meant to stop the scroll. Quote it (lightly trimmed) from the first body. null if there is no body text. Max 300 chars |
| `proposition` | The core promise in your own words, one sentence: what the customer gets and why this brand. Max 300 chars |
| `offer.type` | One of `none, discount, free_shipping, free_gift, bundle, trial, sample, price_point, bogo, loyalty, other`. Use `price_point` when a price is shown with no discount. Use `none` when nothing is offered |
| `offer.text` | The offer exactly as stated ("20% off all facial oils, code GLOW20"). null when `type` is `none` |
| `offer.deadline` | The stated end ("31 January", "this weekend"), or an ISO date if the text is explicit. null if none is stated |
| `cta` | The call to action in the copy or link description ("Shop now", "Book a consultation"). null if there isn't one. Don't guess the button label, because the API does not return it |
| `tone` | 1-3 values from: `urgent, confident, warm, playful, educational, aspirational, luxurious, clinical, authentic, calm, celebratory, empathetic, salesy`. Definitions are in `prompts/rubric.md` |
| `audience_signals` | Up to 5 short phrases describing who the copy speaks to ("dry-skin sufferers", "gift buyers"). Only what the text supports |
| `format_guess` | `carousel` if there are several distinct card bodies or titles; `multi_variant` if there are several near-identical bodies (A/B copy); `video` if `media_type_hint` is VIDEO or the copy refers to watching; `single_image` if the hint is IMAGE or MEME; `text_only` if the hint is NONE; otherwise `unknown` |
| `quality` | Scores `clarity`, `specificity` and `brand_consistency` (integers 1-5) as defined in `prompts/rubric.md`, plus `notes` (one or two sentences, max 400 chars) |
| `confidence` | 0-1: how sure you are about the whole object. Keep it below 0.6 when the text is under 8 words or is not in English |

## Rules

- Use only the text you were given. Do not invent prices, deadlines, claims or audiences.
- Keep quotes short, and never copy an entire long body into any field.
- Non-English copy: fill the fields in English and quote the hook in its original language.
- Judge `brand_consistency` against the dominant voice of this advertiser's ads **in this batch**. If the batch is too small to tell, use 3 and say so in `notes`.
- Near-duplicates (`derived.near_duplicate_of` is set) still get their own full object. Mention the reuse in `notes`.
- Never write URLs other than the ones already present in the input text.

## Output file

Write exactly this shape to the output path you were given (for example `output-01.json`):

```json
{
  "prompt_version": "extract-v1",
  "extractor_model": "<your model alias, e.g. sonnet>",
  "results": [
    {
      "library_id": "990000000000102",
      "llm": {
        "hook": "New year, new ritual",
        "proposition": "Start the year with a facial-oil ritual at a discount",
        "offer": {"type": "discount", "text": "20% off all facial oils, code GLOW20", "deadline": "2026-01-31"},
        "cta": "Shop now",
        "tone": ["celebratory", "urgent"],
        "audience_signals": ["new-year resolvers", "skincare enthusiasts"],
        "format_guess": "single_image",
        "quality": {"clarity": 4, "specificity": 4, "brand_consistency": 4,
                    "notes": "Clear seasonal hook; code and end date stated."},
        "confidence": 0.85
      }
    }
  ]
}
```

Return one result per input ad, in input order, with `library_id` copied exactly.
