You are classifying social media posts from the peptide, longevity, and biohacking niche.

Given the post data below, return a JSON object with these exact fields:

- hook_type: one of exactly these values:
  curiosity_gap | fear_based | social_proof | transformation | educational | controversy | personal_story

- topic: one of exactly these values:
  peptide_education | weight_loss | anti_aging | biohacking | functional_medicine | recovery | hormones | spiritual_health | longevity | general_wellness

- format_guess: one of exactly these values (infer from caption tone and content):
  talking_head | text_overlay | b_roll | before_after | testimonial | lab_science | lifestyle | podcast_clip

- hook_text: the first 8-12 words of the caption that function as the hook. If caption is empty, return empty string.

POST DATA:
{{POST_JSON}}

Rules:
- Return ONLY valid JSON, no explanation, no markdown
- If caption is empty or non-English, still return valid JSON with best guesses
- hook_type = curiosity_gap if caption starts with a question or "what if" / "did you know"
- hook_type = fear_based if it references risk, aging, disease, decline
- hook_type = social_proof if it references patients, results, testimonials, numbers
- hook_type = transformation if it shows before/after or change over time
- hook_type = educational if it explains a mechanism, protocol, or science
- hook_type = controversy if it challenges mainstream advice
- hook_type = personal_story if first-person narrative about their own experience
