# Banding diagnostic, title-based run

Oud 2018 and Walker 2018 returned no confirmed hits. This checks whether that is because topical matches were retrieved and then labelled "maybe", or because the included records were not retrieved at all.

The check uses the set the product already built. It does not change banding in the product, and it does not run a second search. Calling every maybe a hit cannot add a document the search did not return.

| review | included records | in the hit band | in the maybe band | not retrieved |
| --- | ---: | ---: | ---: | ---: |
| Oud 2018 | 20 | 0 | 1 | 19 |
| Walker 2018 | 762 | 0 | 19 | 743 |

The maybe band is the whole retrieved set: 52 documents for Oud, 58 for Walker. One of Oud's 20 included records is in it, and 19 of Walker's 762. The rest were never retrieved.

So the hit rule is why the confirmed count is zero: a confirmed hit has to be a verified name, and these questions produced none. It is not why recall is about 5% and 2.5%. Counting the topical tail as hits leaves those figures where they are. The failure is retrieval.
