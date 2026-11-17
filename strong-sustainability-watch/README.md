# Strong Sustainability Watch

A running list of new articles on strong sustainability in 32 business and sustainability journals, each linked to the strong-side theories of the sustainability matrix (Demastus, Article 10, Figure 1).

Page: https://profdrjdemastus.github.io/strong-sustainability-watch/

## How it runs

The workflow `.github/workflows/strong-sustainability-watch.yml` runs `sweep.py` every Monday at 05:17 UTC and can also be started by hand from the Actions tab. The script needs a free OpenAlex API key stored as the repository secret `OPENALEX_API_KEY`.

Each sweep queries OpenAlex for articles and reviews in every ISSN of every journal in `config/journals.json`, looking back 45 days from the previous sweep so late-indexed articles are caught. The first sweep back-fills from the date in `config/scoring.json`.

## Relevance

Scored 0 to 10 with the dictionary in `config/scoring.json`, the same method as the earlier HTML tool. Core terms in the title score 2.5 each (up to two), core terms in the abstract or keywords score 1 each (up to three), each topic category touched adds 0.6 (up to five), articles with no core term lose 1.5 per counter-dictionary hit (up to two), and each matched matrix theory adds 0.75 (up to 1.5). Articles at or above the threshold join the list.

Compared with the HTML tool, ambiguous core terms were narrowed so an unattended list stays clean. Bare "regeneration" and "regenerative" no longer count as core (they caught chemistry papers on adsorbent regeneration), "steady state" counts only as steady-state economy or economics, "sufficiency" ignores self-sufficiency, and absolute sustainability and circular society were added as core terms.

## Theory links

`config/theories.json` holds the 13 strong-side entries of Figure 1, each with distinctive phrases, optional pairs of ideas that must appear together, and anchor works cited in the matrix article. An article is linked to a theory when its title, abstract or keywords match, or when its reference list cites an anchor work. Each link records why it was made, and the page shows that reason.

## Data

- `data/articles.json` holds the running list.
- `data/runs.json` is the search log, one record per sweep, for audit trails.
- `data/anchors.json` caches the anchor works resolved to OpenAlex ids.

Editing a config file changes how earlier entries are scored and tagged at the next sweep. Entries are never removed automatically.
