# Detecting AI vs. Human Text: Provenance, Watermarks, and Statistical Methods

**Executive Summary:** Industry and standards bodies are rapidly converging on *transparent provenance* and *watermarking* for AI-generated text. For example, the EU AI Act’s transparency code requires machine-readable marking of synthetic outputs; Anthropic’s Claude (post-Aug 2026) will embed imperceptible watermarks in text and attach C2PA content-credential metadata to files. Google DeepMind has open-sourced its *SynthID-Text* technology (a secret-key, distributional watermark) and plans to roll it into Gemini. OpenAI has likewise adopted the C2PA Content Credentials standard (for images/audio) and plans future text support. 

This report **inventories** provenance/metadata standards (e.g. C2PA manifests, signed tokens like the NeurIPS’ *Bileve* scheme), **catalogs** watermarking methods (token-level vs. semantic, robust vs. fragile, secret-key schemes, e.g. SynthID vs. the embedding-space “PASA” watermark), and lists **free/paid tools** for each pipeline stage (metadata readers, watermark detectors, AI-text classifiers). We compare tools on cost, API, accuracy, latency, privacy, integration, and limitations.  For each detection step, we outline implementation plans (sample code, compute needs, libraries).  Finally, we analyze statistical/ML detection techniques (perplexity, stylometry, embeddings, ensembles) and adversarial robustness (paraphrasing, editing, model-mismatch), with recommended thresholds and evaluation datasets. A flowchart (Mermaid) of the pipeline is provided, along with example report formats and human-review guidelines.

## 1. Provenance & Metadata Standards

- **C2PA Content Credentials (Open Standard):** The *Coalition for Content Provenance & Authenticity (C2PA)* defines an open framework to attach signed metadata (assertions) to digital media. Fields include creator, creation time, tools used, etc. OpenAI, Anthropic and others use C2PA for images/audio; OpenAI even provides an [online verify tool](https://openai.com/verify) for supported images/audios. Anthropic’s Claude will embed C2PA manifests in any generated file (PNG, SVG, audio, etc). Free tools (e.g. [C2PA Python SDK](https://opensource.contentauthenticity.org/), [Adobe’s content authenticity SDKs]) can read or verify these manifests. For text (e.g. DOCX, PDF) there is no widespread C2PA support yet, but the same libraries (e.g. `c2pa` PyPI) can extract credentials if present. Paid: enterprise platforms (e.g. Adobe Experience Manager Assets) also verify C2PA. **Implementation (free):** use the [c2pa-python](https://pypi.org/project/c2pa-python/) library to parse and verify manifests in an input file; e.g. `c2pa = c2pa.Manifest.verify(file)` then check `c2pa.signing_cert`. *Compute:* trivial CPU, I/O. *Privacy:* on-device only, no data sent out. *Limitation:* only works on supported file types (images, audio, video, some docs), not plain text strings.

- **Cryptographic Signatures / “Model-Signed” Tokens:** A few research proposals embed digital signatures into the text stream. For example, Zhou et al. (NeurIPS 2024) propose **Bileve**, a bi-level signature scheme that inserts fine-grained signature bits (at the token level) plus a coarse watermark for fallback. At each token, a signature bit is embedded (break on tampering); if signature verification fails, a larger-scale watermark signal indicates possible origin. *Standardization:* none yet, but such schemes would require new LLM architectures. *Providers:* none public yet; open research only. **Implementation:** would require model modification to output (and verify) signatures. *Status:* experimental (NeurIPS 2024, [open-source code](https://github.com/google-deepmind/synthid-text) only for SynthID, no public Bileve toolkit).

- **Provenance Headers / Metadata Fields:** Beyond C2PA, some vision of “AI provenance header” is emerging. For images, providers include EXIF/IPTC tags (Anthropic uses IPTC in tandem with C2PA). In text, there is no standard header (plain text has no embedded metadata). Some proposals for text “metadata” include hidden tokens or zero-width characters, but these are not standardized. **Implementation:** could add a JSON header to a file container (e.g. DOCX Custom XML, PDF metadata) or prefix special tokens to plain text (if interoperable). No major provider has announced text metadata beyond watermarks as yet.

- **Standards Bodies & Legislation:** Key standards include C2PA (content provenance) and IPTC for media. The EU’s **AI Act Article 50** and associated *Transparency Code of Practice* effectively make provenance/watermarks mandatory for deployed LLM systems in the EU. Over 190 organizations (OpenAI, Google, Microsoft, Amazon, Mistral, Cohere, etc.) have signed on (Meta and xAI have not). The Code calls for *machine-readable marking* of AI-generated outputs. This is driving providers to embed watermarks and metadata globally, not just in EU.

## 2. Watermarking Techniques

Watermarking methods aim to embed an **imperceptible signal** in generated text. They vary in approach:

- **Distributional (Probabilistic) Watermark:**  The current industry frontrunner is *SynthID*-style watermarks. A secret key deterministically biases the model’s token sampling: at each step, the generator slightly reweights some subset of tokens (blue-highlighted in the example below) to encode a pattern.  Detection uses statistical tests on the token distribution (e.g. weighted mean or Bayesian detectors) to compute a “watermarkedness” score. Google DeepMind’s SynthID-Text is an example (open-source code on [GitHub](https://github.com/google-deepmind/synthid-text), [PyPI package](https://pypi.org/project/synthid-text/)). It reports a probability of being watermarked (e.g. *“Probability of being watermarked: 99.9%”* in the figure below). This method is *robust* to minor edits (small changes/cr-p/paste) but can break under heavy paraphrasing or translation.

 *Figure: Google’s SynthID text watermark highlights (blue) statistically-selected tokens in a generated email and reports “Probability of being watermarked: 99.9%”. This secret-key distributional watermarking is designed to survive moderate copy-paste but, as shown by Meta’s threat analysis, can be removed by strong paraphrasing.*

- **Token-Level (Flag/Prefix) Watermark:** A simpler approach is to insert invisible characters or rare tokens. For example, one could append zero-width Unicode characters as a pattern. This is *fragile* (one paste-strip or rewrite removes it). OpenAI has stated *it is not using visible watermarks on text*, and it would be privacy-invasive to embed identifying tokens. No major text model uses this in production.  (For images, a visible watermark or EXIF/IPTC tag is possible; GPT-4Vision even allows the prompt “Include a visible watermark.”, but text is opaque.)

- **Embedding-Space Watermarks:** Recent research (e.g. *PASA*, May 2026) proposes watermarking in semantic embedding space. PASA clusters tokens by meaning and embeds a joint signal at the semantic level, aiming to be **robust to paraphrase**. In theory, it significantly outperforms vocabulary-based watermarks under synonym swaps. This is still academic; no deployed service uses PASA yet. (Meta’s open-source *Meta Seal* initiative includes image/audio/text watermarking APIs, but as of 2026 text support is still forthcoming.)

- **Selective Exposure / Stylometric Watermark:** Another idea is to tweak higher-level aspects (syntax complexity, rarer synonyms) to watermark style. These are essentially fragile “stylometric” marks. Providers have not used these in outputs (though researchers have studied stylometric signatures).

- **Combined Approaches:** Anthropic’s Claude will use a combination: invisible token-level watermark *plus* cryptographic provenance metadata on files. Similarly, OpenAI’s image pipeline uses C2PA metadata *and* SynthID watermark. Meta’s threat model explicitly uses three layers: metadata (C2PA/IPTC), invisible watermark (like SynthID), and classifier-based detection.

## 3. Detection Tools and Services

The detection pipeline has four main steps: (A) Provenance/metadata check, (B) Watermark detection, (C) Statistical/ML classification, (D) Human review. Below we list representative tools for each step. A comparative summary (Table) follows.

### 3.1. Provenance/Metadata Verification Tools

- **OpenAI Verify API (images/audio):** Free web/REST API (requires OpenAI account) to check C2PA/SynthID signals in uploaded files. *Formats:* Supported image formats (PNG, JPG, etc) and audio (WAV, MP3). *Output:* Indicates OpenAI provenance, trusted metadata. *Accuracy:* Guaranteed for supported cases; not available for text yet. *Privacy:* Files are analyzed server-side; not stored per OpenAI’s policy (see docs). *Integration:* REST API with JSON output. *Limitations:* No text support; only OpenAI signals (won’t detect Anthropic/other marks).

- **Anthropic Claude Content Checker (images):** Free browser tool (privacy-preserving, on-device) to detect C2PA credentials on images/audio. *Formats:* JPG, PNG, GIF, video, audio, etc. *Output:* “Made with Claude or processed by Claude.” *API:* No public API (for files, only web UI). *Accuracy:* Reads the signed manifest; 100% for files with Content Credential. *Limitations:* Does not check text (text detection API is private). *Privacy:* Runs locally (file never uploaded).

- **C2PA Open-Source Libraries:** Free SDKs (Python, JS) for creating and verifying C2PA manifests (see [25] “Official Tools & Libraries”). *Integration:* e.g. `from c2pa import verify`; read manifest from file. *Compute:* negligible. *Limitations:* Only image/audio/video/PDF.

### 3.2. Watermark Detection Tools

- **SynthID-Text (DeepMind Reference Library):** Open-source code and HF Transformers implementation. Can *both embed and detect* watermarks for compatible models. *Usage:*  
  - Embed: wrap a HuggingFace model with SynthID mixin (`GemmaForCausalLM+SynthIDMixIn`).  
  - Detect: use the library’s Weighted-Mean or Bayesian detector classes.  
  - *Compute:* Requires GPU (16–32GB) for large models (Gemma2). Detection is faster (just forward pass with statistics).  
  - *Accuracy:* Very high for long text (near-100% when embedding key is known, as Google reports).  
  - *Limits:* Only works if the text was watermarked with the same secret key. Cannot detect watermarks from unknown schemes.

- **Google/Anthropic Watermark API (private):** Anthropic will offer a paid private *Claude Watermark Detector API* for regulated entities. (Likely similar to SynthID detect, requiring key.) No public pricing or interface known. OpenAI has not announced a text watermark API yet.

- **Meta Seal (Open-Source):** Meta’s *Seal* is a research project for embedding watermarks (images/audio/video/text). The GitHub ([facebookresearch/meta-seal](https://facebookresearch.github.io/meta-seal/)) includes code. *Text:* Currently under development; might allow watermarking at model time. *Detection:* Unclear if text detection code is available. *Status:* Experimental.

- **Opace AI-Content Checker (Open Source):** A free toolkit (WordPress plugin, CLI) that checks for hidden watermark characters and content authenticity. Claims to detect hidden Unicode tokens and test for known watermarks (SynthID-Text lab) and C2PA. *Accuracy:* User reports only. *Integration:* Python CLI / JS libraries. *Limitations:* Possibly incomplete; more community tool.

### 3.3. Statistical/ML Classifiers (AI-Text Detectors)

Many commercial and open-source services analyze text features (perplexity, n‑gram patterns, stylometry) to score AI-likeness:

- **GPTZero:** Freemium web tool focused on education. *Input:* Paste text or upload DOC. *Output:* “Human Score” vs “AI Score” with “perplexity” metrics. *Accuracy:* Anecdotally high; Pangram reported 100% on small tests. *Cost:* Free basic; paid API is offered (tiers unknown). *Privacy:* Claims no training use. *Limitations:* Proprietary, only English, results can be gamed by high-level editing.

- **Originality.ai:** Paid SaaS for content publishers. *Input:* Web form or API (supports HTML/TXT). *Output:* % AI probability, plagiarism check. *Cost:* Paid ($20/125k words). *Claims:* ~98% accuracy vs published benchmarks (no independent citable data). *Privacy:* States content not added to training corpora. *Integration:* REST API. *Limitation:* Proprietary, closed model.

- **Turnitin (SecureWrite):** Paid academic service (institution license). *Features:* Integrated into plagiarism checker, reports AI usage flags. *Accuracy:* Turnitin cites research (e.g. 93% accuracy on essays). *Privacy:* Student submissions are confidential; no model training. *Limitation:* No public API, only institutional SaaS.

- **Copyleaks AI Content Detector:** Freemium tool for publishers. *Input:* Text, DOCX, HTML. *Output:* AI probability, per-sentence tags. *API:* Yes (REST). *Cost:* Free for small uses, then paid ($0.60 per page for business plan). *Privacy:* States content not stored. *Accuracy:* Pangram ranked it high (passed their tests). *Latency:* Fast (online API). 

- **Quillbot AI Detector:** Free web tool (also offers grammar/paraphrase). *Input:* Paste up to 1200 words (free). *Output:* % likelihood, with highlighted “explainer” cards. *Pricing:* Free limited (6 scans/day); Premium removes caps. *Claims:* ~99% detection rate on RAID benchmark. *Privacy:* Claims “privacy-first design” (keeps text secure). *Methods:* Uses predictability, sentence variation, repetitiveness cues. *Limitation:* Black-box, long text only, might over-flag polished writing.

- **GLTR (OpenAI/Harvard, 2019):** Open-source research tool. *Method:* Compares text token probabilities under a reference model (GPT-2). *Output:* Heuristic “heatmap” (rare tokens flagged). *Accuracy:* Good for simplistic LLMs (like GPT-2) on short snippets, not robust to modern paraphrases. *Integration:* Python package, demo UI. *Limitation:* Outdated (GPT-3+ undetected).

- **DetectGPT / Grover / IIT-Monash Models:** Some published detectors (e.g. Stanford’s DetectGPT, Grover) are available as open code. These are model-specific, requiring likelihood scores from candidate LMs. *Integration:* Research code, not user-friendly.

- **EmergentMind DetectAnyLLM:** Advanced research approach (Direct Discrepancy Learning) promising strong cross-model detection. Not a commercial product yet, but indicates the future: an ensemble of perturbation-based scores, stylometry, and watermarks boosts robustness. 

**Table 1:** *Tools/Services for Each Pipeline Step (comparative).*

| **Tool/Service**      | **Step(s)**                | **Cost**           | **Input/Output**        | **Accuracy/Benchmarks**            | **Latency**  | **Privacy/Data**              | **Integration**                   | **Limitations**                             |
|-----------------------|----------------------------|--------------------|------------------------|-------------------------------------|--------------|-------------------------------|------------------------------------|---------------------------------------------|
| *OpenAI Verify*    | Metadata (images/audio)   | Free               | Upload image/audio → labels (OpenAI provenance) |  Supported formats: PNG, JPG, WAV. Exact match with C2PA/SynthID; no known metrics. | Seconds (web) | Processed on OpenAI servers; not stored. | Web UI/REST API (JSON)             | No text support; only OpenAI-origin signals. |
| *Claude Content Checker* | Metadata (images/audio)   | Free (web UI)      | Drag image/audio → Claude provenance |  Reads C2PA metadata; 100% for valid Claude credentials. | Immediate (browser) | On-device only (file not uploaded).  | Browser; no public API        | No text checking (private API).             |
| **C2PA Toolkit** (open-source) | Metadata parsing/verification | Free               | File in → extract assertions  | N/A (tool, not classifier)           | Fast CLI   | Local only                      | Python/Node SDKs, CLI            | Only works on media/containers (not raw text). |
| **SynthID-Text (HF)** | Watermark detection & embedding | Free (open code)   | Text (tokens) →  watermark flag/prob | ~100% on long generated text (per DeepMind) | Fast (model forward) | Local/GPU; model inference only    | Python (pip), HF Transformers mix-in | Requires watermark key and model hash; not for unknown watermarks. |
| **Anthropic Watermark API** | Watermark detection      | Private (regulated) | Text → yes/no (Claude mark)  | Not publicly benchmarked            | (unknown)   | Enterprise (private preview)       | REST API (requires approval)      | Restricted access; EU-only initially.      |
| *Meta Seal (prototype)* | Watermarking (various) | Free (open source) | TBD (images/audio/text)    | Early-stage; not field-proven        | TBD         | Local (open source)               | Source code (GitHub)             | Text watermark support still in R&D.      |
| **Pangram Labs**  | Classifier (text)       | Freemium          | Text → AI/Human %  | Pangram claims 100% on their tests (9/9) | ~1s (API)  | Unknown policies (likely does not use data) | API, Chrome/Canva plugins        | Pricing ($15 for 600 docs); commercial bias possible. |
| **GPTZero**      | Classifier (text)       | Freemium (education) | Text/Doc → Human% / AI% | High (their site reports 97%+ on ChatGPT essays) | ~1–5s (web) | No training data usage claimed  | Web UI, API (paid)               | Not open; accuracy on short text or non-English is low. |
| **Originality.ai**          | Classifier (text + plagiarism) | Paid ($)         | Text/HTML → AI% + plagiarism | Vendor claims ~98% (no independent data)   | Fast (cloud) | Doesn’t retain content (per docs)   | API (JSON), Wordpress plugin       | Paid only; US-centric; no free tier.     |
| **Turnitin SecureWrite**    | Classifier (academic)   | Paid (institution) | Text upload → AI flag + report | Vendor reports ~90% on essays; high false-negative on short content. | Varies (cloud) | Confidential storage by school      | LMS integrations (no public API)  | Only for enrolled institutions; closed source. |
| **Copyleaks**              | Classifier             | Freemium          | Text/DOC → AI%  | Pangram gave high marks (9/9) | <10s (API) | Data not stored (per TOS)         | API, CLI, Web UI                | Plagiarism check included; free tier limited. |
| **Quillbot Detector**  | Classifier             | Freemium          | Text (1,200w max) → 0–100% | 99% on RAID benchmark (per Quillbot) | ~1s (web)  | Local scans (text not reused)    | Web UI                         | Limited free scans; black-box model.     |
| *Open-Source Detectors:* GLTR, DetectGPT, etc. | Classifier (dev tools) | Free/open         | Text → scores/flags    | Varies (e.g. GLTR ~70% on older models) | Fast (local) | Fully local (data safe)            | Python libraries/notebooks       | Require technical expertise; outdated for current LLMs. |

*(See sources: OpenAI, Anthropic, Pangram test results, Quillbot site.)*  

## 4. Implementation Plan by Pipeline Step

Below we outline technical steps, sample code/algorithms, and options (free vs. paid) for each detection stage.

### 4.1 Provenance/Metadata Check

1. **Check for C2PA/Content Credentials:**  
   - *Free:* Use the C2PA Python SDK (`pip install c2pa`). Load the input (e.g. image/audio file) and attempt to extract a manifest.  
     ```python
     import c2pa
     manifest = c2pa.Manifest.from_file('input.png')
     if manifest and manifest.verify():
         print("Found signed provenance:", manifest.get('tool'), manifest.get('created'))
     ```  
     This verifies the cryptographic signature and reads metadata claims.  
   - *Paid/Service:* Call OpenAI’s Verify API (images/audio only) via REST (requires auth key).  
     Example (pseudo):  
     ```
     curl -H "Authorization: Bearer $KEY" -X POST https://api.openai.com/v1/verify-content \
          -F file=@input.png
     ```  
     Response JSON indicates presence of SynthID/C2PA for OpenAI.  

2. **Check for Model-Specific Signatures:**  
   - If using a service like Anthropic, one could call its (private) text detect API if available. For example:  
     ```
     # (Hypothetical) POST https://api.anthropic.com/v1/watermark_detect 
     { "text": "..."} 
     ```  
     This would return `"watermarked": true/false` and maybe model ID. (Not publicly documented yet.)  
   - *Free:* There is no public token-signature verifier for text. If Bileve or similar existed, one would run a verification algorithm (binary checks) using the public key.

3. **Decision:** If a **trusted signature/metadata is found**, immediately label accordingly: e.g. “AI-generated by Model X (signature valid)”. If it explicitly labels human, trust it. If metadata is absent or inconclusive, proceed to watermark detection.

### 4.2 Watermark Detection

1. **SynthID Detection (for Google/Anthropic style watermarks):**  
   - *Free:* Install the SynthID library or Transformers integration. For Python:  
     ```python
     from synthid_text import WeightedMeanDetector, SynTHoEmbeddingWatermark  # etc.
     text = open('input.txt').read()
     detector = WeightedMeanDetector(key_id='mykey')  # or load default
     score = detector.detect(text)
     print("AI watermark score:", score)  # e.g. 0.99 means likely watermarked
     ```  
     (The actual API uses language model log-probs; see [31] for usage.)  
   - *Compute:* Requires running the generation log-probabilities (may need GPU for large LMs). The Github notes a T4/A100 GPU for 2B–7B models.  
   - *Paid:* Use Anthropic’s Detection API when it’s available (sign up via Anthropic). It would take text as input and output { “watermarked”:bool, “model”:string }.  

2. **Other Watermarks:**  
   - If text watermarks other than SynthID (e.g. proprietary) exist, detection is proprietary (no open tool).  
   - *Visible/Hidden Markers:* Check for odd invisible Unicode (zero-width) or known marker sequences (free regex/substring check). This is not standard; providers generally do not use such.

3. **Decision:** If a watermark is detected with high confidence, **flag as AI-generated** and note provider/model if known. Otherwise, go on to feature analysis.

### 4.3 Statistical Feature Extraction & Classification

1. **Compute Language Model Perplexity / Likelihood:**  
   - *Free:* Use an open LLM (GPT-2 or GPT-Neo) to score the text. Many detectors work by computing the text’s average log-prob under a model (AI text tends to have higher/lower perplexity depending on model). Example with HuggingFace:  
     ```python
     from transformers import GPT2LMHeadModel, GPT2Tokenizer
     model = GPT2LMHeadModel.from_pretrained('gpt2')
     tok = GPT2Tokenizer.from_pretrained('gpt2')
     tokens = tok.encode(text, return_tensors='pt')
     loss = model(tokens, labels=tokens).loss  # cross-entropy loss
     perplexity = torch.exp(loss).item()
     ```  
     Compare perplexity to a threshold. (LLM outputs often have *lower perplexity* under similar models).  
   - *Paid:* Call a commercial API that returns logprobs, e.g. `openai.Completion.create(..., echo=True, logprobs=1)` to compute average logprob.  

2. **N-gram Statistics & Surprise Features:**  
   - Compute e.g. 4-gram overlap with a typical human corpus or measure “burstiness” (variance of token surprisals across the text). Basani et al. find that **human text has more fluctuation in surprisal** than AI text. One can compute for each token:  
     $$ s_i = -\log P(w_i|w_{<i}) $$  
     then measure variance of $s_i$ across the document. Human text tends to have higher variance. (See the *DivEye* paper for details.)  
   - Many detectors (e.g. Quillbot) explicitly use features like predictability and sentence-structure variation.

3. **Stylometric Features:**  
   - Extract features like average sentence length, word rarity (common vs rare words), function word frequencies, punctuation patterns. Tools like *Electron* or *stylo* (R) can do this. LLM-generated text often uses more common vocabulary and punctuation patterns.  
   - Embed each sentence with a sentence-transformer (e.g. SBERT) and measure the *distance from typical human writing embeddings*. Human-authored text may occupy a different subspace than LLM outputs (see DetectAnyLLM’s fingerprinting).

4. **Classifier Ensemble:**  
   - Combine the above signals into a binary classifier. Free option: train a simple logistic regression or neural net on labeled data (MIRAGE benchmark, ACLCW-benchmarks, etc.). E.g. features = [perplexity, vocab-rarity, surprisal-variance, stylometric metrics].  
   - Paid: use SaaS detectors (GPTZero, Originality) via API calls. They encapsulate such models internally.  
   - *Thresholds:* Calibrate on held-out human/AI corpora. For example, set AI-threshold so that false-positive rate (human misclassified) is <5%. If score ≥ threshold, classify as AI-likely.

5. **Output:**  
   - If classifier **score ≥ threshold**, flag “Likely AI-generated” (but with confidence score). Else, “Likely Human.”  (Note: absence of evidence is not evidence of absence; see [30†L115-L124] on limitations.) 

### 4.4 Human Review and Reporting

- **Confidence Scoring:** Report detection results as probabilities or confidence intervals. E.g. “AI-likelihood: 92% (high confidence)” or “Human-likelihood: 88%” based on calibrated classifier output. Quillbot’s UX (0–100%) is a good model.
- **Explanation:** Include key signals: “Watermark from Anthropic Claude detected” or “Perplexity = 12.3 (low)” or highlighted suspicious sentences. Some tools (Quillbot) provide explainers for flagged segments.
- **Human Review:** If flagged as AI, recommend manual check: e.g. verify factual content, coherence. If short text (<100 words) or borderline score, human review is essential (short passages are hard to classify). Provide guidelines:
  - Verify style against known authorship. Highly polished, generic text might require rewriting (per [28†L85-L94] advice).
  - If watermark flagged, treat as likely AI but consider context (maybe user used AI to edit text). Conversely, if classifier flagged but watermark absent, double-check threshold (AI Act suggests caution vs. simply trusting classifier alone).
- **Flagging:** For flagged content, generate a report line. Example:  
  > *“Analysis:* SynthID watermark detected (Claude). Classified as AI-generated with probability 99.9%. Human review recommended. *Footnotes:* Score calibrated for 95% confidence threshold. No guarantee on content accuracy.”*  
  For non-flagged:  
  > *“Analysis:* No provenance/watermark found. Classifier score = 12% AI-likelihood (below threshold). Reported as likely human-authored. *Note:* If user edits raw AI output, watermark may be removed; review style and provenance if in doubt.”*

## 5. Statistical/ML Detection Details

- **N-gram / Lexical Patterns:** Early detectors examined irregular n-gram frequencies (e.g. rare bigrams that an LLM might avoid). Modern detection instead uses model-based likelihoods. Still, a surprise (“burstiness”) metric can be computed: Basani et al. (2025) show that variance of token surprisal across a text is a strong signal (human text has “richer variability” than LLM output). Implementation: slide a window or compute sentence-level entropy. Use as feature or directly flag if below a threshold.

- **Perplexity / Entropy:** Compute the text perplexity under a reference LM (lower perplexity = more “AI-like”). Studies (and tools like Quillbot) report that AI text is generally more predictable. Report values or compare to a distribution of human text scores. Example threshold: if perplexity < 50 under GPT-2, flag as AI (tune per-domain). 

- **Stylometry:** Features like function-word frequencies, average word/sentence length, punctuation rate, Hapax Legomena (unique words) count, POS tag distributions. Combine them in a supervised detector. E.g. an SVM on stylometric vectors. (However, pure stylometry is weak if author is unknown and LLMs mimic style.)

- **Embedding Distance:** Compute embeddings of the entire text (e.g. with OpenAI’s Embeddings API or SBERT). Compare distance to centroid of known human-written corpora vs. to an LLM-text centroid. A large positive “AI distance” suggests generated text. Or train a simple nearest-centroid classifier in embedding space.

- **Ensemble Approaches:** As EmergentMind (DetectAnyLLM) suggests, best practice is to combine signals: perplexity under multiple models (GPT-2, GPT-J, etc.), stylometric scores, watermark result if any. Ensembles (e.g. random forest on all features) are more robust to overfitting and model shifts.

- **Calibration:** Use metrics like ROC-AUC, F1 to set thresholds. For example, choose threshold so that 95% of known-human texts are below it (limiting false-positives). Then measure true-positive rate on held-out AI texts. Preferred metrics: *Balanced Accuracy, AUROC, Precision@someRecall*.  

- **Datasets for Testing:** Use diverse benchmarks, e.g. MIRAGE (17 LLMs, 5 domains), plus domain-specific sets (student essays, news articles) and adversarial sets (prompt-finetuned paraphrases). Also the GLTR/Real-or-fake dataset (Wikimedia articles), and translation/paraphrase corpora (e.g. English-French-English roundtrip). Ensure to include short (<50 words) and long texts. Track performance under text editing: e.g. for each AI sample, test after “translation” or GPT-2 paraphrasing to gauge robustness.

- **Adversarial/Robustness:** Major threats are paraphrasing (as Meta’s analysis notes) and "regeneration" (feeding AI text into another model). No watermark survives heavy paraphrase; statistical cues degrade too. Robust methods (like PASA) aim to resist paraphrase by tying watermark to meaning. Evaluate by applying strong paraphrasers: e.g. use back-translation (translate to another language and back), or instruct a rival LLM to “rewrite”.

## 6. Adversarial Considerations

- **Paraphrasing/Editing:** Simple synonym swaps or sentence reordering can collapse token-level watermarks. Meta’s model warned that paraphrasing can drop SynthID detection from ~100% to chance. Robust detection therefore relies on multiple signals: if watermark fails, classifier may catch generic phrasing. Conversely, editing AI text to mimic human style (increasing perplexity) can fool detectors. 

- **Model-Mismatch:** Zero-shot detectors (like GPT-2 perplexity) may struggle if the AI text comes from a radically different model. Fine-tuning detectors on one model can degrade on another. Thus, use model-agnostic or ensemble detectors (e.g. DetectAnyLLM’s direct discrepancy learning).

- **Noise Attacks:** Small typos or unicode homographs might trick detectors. Ensure normalizing text (lowercasing, removing non-word chars) before analysis. Many detectors ignore case/spacing.

- **Threshold Tuning:** Because stakes can be high, calibrate thresholds for each context. In sensitive domains (legal, hiring), err on side of flagging (low false-negative), and route more samples for human review. In open publishing, avoid false positives (flag only high-confidence AI).

## 7. Pipeline Flowchart and Reporting

```mermaid
flowchart TD
    A[Input Text] --> B{Check Provenance/Metadata}
    B -- Metadata Found --> C[Verify Signature; Label Source AI/Human]
    B -- No Metadata/Inconclusive --> D[Watermark Detection]
    D -- Watermark Detected --> E[Flag as AI-generated (with provider tag)]
    D -- No Watermark/NA --> F[Compute Statistical Features]
    F --> G{Run Classifier + Thresholds}
    G -- AI-Score ≥ Threshold --> E
    G -- AI-Score < Threshold --> H[Likely Human*]
    E --> I[Report: AI (probability/confidence)]
    H --> J[Report: Human-Likely]
    I --> K[Optional: Human Review for Edge Cases]
    J --> K

    subgraph Note
        style Note fill:none,stroke:none
        K[ *“Human-Likely” means no strong AI signal detected. ]
    end
```

**Example Report Outputs:**  
- *AI-Generated:* “Watermark from Claude 5.1 detected; model confidence 98%. No metadata. Classifier score = 0.92 (above 0.9 threshold). Output labeled **AI-generated (high confidence)**. Recommend human review of content.”  
- *Likely Human:* “No watermark or signature found. Classifier AI-score = 0.12 (below 0.5 threshold). Reported as **likely human-written**. (Note: short text, classifier uncertain; if correctness is critical, consider deeper review.)”

**Human-Review Guidelines:** Flagged AI content should be checked for context and editing: e.g. verify citations and logical flow. If classifier alone flagged text, examine for formulaic phrasing. Encourage writers to explicitly *cite AI use* if any (per academic integrity guidelines). Remember detectors can err; use them as a signal, not a verdict.

---

**Sources:** Recent provider documentation and research papers as cited above. All technical details rely on up-to-date 2024–2026 publications and standards.