# SYNERGY retrieval-assurance evaluation

Retrieved set for coverage, elusion and the required sample size is hits and maybes, which is the set the methodology says it is aiming to recall and the pool its elusion sample is drawn from. True recall is also shown for hits alone. The required sample size is the smallest elusion sample whose 95% interval lies within ±20% of the true elusion rate (sampling without replacement). That size is not capped at 5,000: the sweep stops there, and a larger figure means 5,000 was not enough. A dash means the rate was zero or undefined.

| review | N | included | coverage (hits+maybe) | coverage (hits) | true recall (hits) | true recall (hits+maybe) | true elusion rate | our estimate | calibration error | required sample size |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Donners_2021 | 258 | 15 | 0.8140 | 0.8101 | 0.9333 | 0.9333 | 0.0208 | 0.0500 | 0.0292 | 47 |
| Nelson_2002 | 366 | 80 | 0.8388 | 0.8361 | 0.9750 | 0.9750 | 0.0339 | 0.1000 | 0.0661 | 59 |
| Oud_2018 | 952 | 20 | 0.0546 | 0.0000 | 0.0000 | 0.0500 | 0.0211 | 0.0500 | 0.0289 | 752 |
| Muthu_2021 | 2719 | 336 | 0.9217 | 0.9161 | 0.9464 | 0.9554 | 0.0704 | 0.2000 | 0.1296 | 187 |
| Hall_2012 | 8793 | 104 | 0.7114 | 0.7012 | 0.9423 | 0.9423 | 0.0024 | 0.0000 | 0.0024 | 2428 |
| Walker_2018 | 48375 | 762 | 0.0012 | 0.0000 | 0.0000 | 0.0249 | 0.0154 | 0.0000 | 0.0154 | 5636 |

We checked the retrieval-assurance method on 6 systematic reviews from SYNERGY (258 to 48,375 records), where every record is already labelled included or not, using each review's title as the question because this version of SYNERGY does not ship one. On 4 of them the search found between 93.3% and 97.5% of the records human reviewers had included; on Oud 2018 (5.0%) and Walker 2018 (2.5%) it found far fewer, because a confirmed hit in this method is a verified name and those questions produced none. The check on what was missed uses a sample of 20: on the one sample actually drawn, the largest gap was 13 percentage points (Muthu 2021: the sample said 20.0%, the truth was 7.0%); on Hall 2012, 6 included records were left among 2,538 not retrieved (a miss rate of 0.24%), and redrawing that sample ten thousand times found none of them in 95.3% of draws. Estimating a review's miss rate to within 20 percent of its true value, 19 times out of 20, would take a sample of 47 to 5,636 records on these corpora, not the 20 the product draws.

What this does not show. SYNERGY is titles and abstracts only, so this tests the assurance method and not the ingestion pipeline. The candidate pool is already the output of a human Boolean search, so coverage is over that pool and not over all literature.

The 95% interval of the miss-rate estimate, for the four reviews whose search found most of the included records, is plotted in [convergence.svg](convergence.svg). The average estimate sits on the true rate at every sample size. The interval is what narrows, and at a sample of 20 the median draw is still zero on Donners and on Hall.

Oud and Walker returned no confirmed hits. [banding_diagnostic.md](banding_diagnostic.md) counts the included records in the maybe band against those never retrieved. One of 20 and 19 of 762 were in the topical tail. Calling those maybes hits does not recover the rest. The product's banding was not changed.
