/** Published brand hues, one per endpoint this app can talk to.

    This map is the only place a vendor colour enters the app. The design language owns its own
    palette (amber accent, green for human, yellow for AI, grey for rest) and a stylesheet cannot be
    allowed to acquire a sixth "brand" colour by accident: `ProviderMark` reads the hue here and
    writes it onto the element as the `--neon` custom property, so a grep over the CSS finds no
    vendor hex, and the guard that parses the stylesheet never has to know what a logo looks like.

    A hue belongs here only if the vendor publishes it. Three of these are traceable: NVIDIA's green,
    Google's four-colour blue (Gemini is served under the Google logo), and OpenRouter's own brand
    page, which names Grape for its mark. An endpoint with no published hue — a gateway the admin
    added, or one whose mark is genuinely monochrome — gets no entry: the mark then falls back to the
    app's own accent when it lights, which is the honest reading of "this endpoint is spendable"
    without inventing a colour for a company that does not use one. */
export const PROVIDER_HUES: Record<string, string> = {
  nvidia: '#76b900',
  gemini: '#4285f4',
  openrouter: '#7624f4',
};

/** Published logo files, keyed by the same slug as the hues above.

    These are the vendors' own marks, not a letter somebody typed: OpenRouter's glyph comes from its
    brand page in the grape colourway, and the NVIDIA eye and the Gemini star are filled with the
    exact hue from `PROVIDER_HUES` so the logo and the light behind it cannot drift apart.

    A provider with no published mark — OmniRoute is self-hosted, and an admin can add any gateway —
    has no entry here, and `ProviderMark` then renders a bare LED dot instead of inventing a logo.
    It never falls back to an initial: a letter inside a circle reads as a brand, and it is not one. */
export const PROVIDER_LOGOS: Record<string, string> = {
  nvidia: '/brand/nvidia.png',
  gemini: '/brand/gemini.png',
  openrouter: '/brand/openrouter.png',
};
