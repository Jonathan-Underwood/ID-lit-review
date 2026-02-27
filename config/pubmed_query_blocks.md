# PubMed Query Blocks

These are the core query blocks used by the generator.

## 1) Journal block

```text
("N Engl J Med"[Journal] OR "Lancet"[Journal] OR "JAMA"[Journal] OR "BMJ"[Journal] OR "Ann Intern Med"[Journal] OR "JAMA Intern Med"[Journal] OR "Lancet Respir Med"[Journal] OR "Lancet Diabetes Endocrinol"[Journal] OR "Lancet Gastroenterol Hepatol"[Journal] OR "Circulation"[Journal] OR "Eur Heart J"[Journal] OR "J Am Coll Cardiol"[Journal] OR "Chest"[Journal] OR "Am J Respir Crit Care Med"[Journal] OR "Intensive Care Med"[Journal] OR "Crit Care"[Journal] OR "Anaesthesia"[Journal] OR "Br J Anaesth"[Journal] OR "Emerg Med J"[Journal] OR "Lancet Infect Dis"[Journal] OR "Clin Infect Dis"[Journal] OR "J Infect Dis"[Journal] OR "Open Forum Infect Dis"[Journal] OR "Euro Surveill"[Journal] OR "Emerg Infect Dis"[Journal] OR "Clin Microbiol Infect"[Journal] OR "J Antimicrob Chemother"[Journal] OR "Antimicrob Agents Chemother"[Journal] OR "mBio"[Journal] OR "Infect Control Hosp Epidemiol"[Journal] OR "Nature"[Journal] OR "Science"[Journal] OR "Cell"[Journal] OR "Nat Med"[Journal] OR "Nat Microbiol"[Journal] OR "Nat Immunol"[Journal] OR "Cell Host Microbe"[Journal] OR "Immunity"[Journal] OR "Proc Natl Acad Sci U S A"[Journal] OR "eLife"[Journal])
```

## 2) Near-term clinical translation block

```text
("phase 2"[Title/Abstract] OR "phase ii"[Title/Abstract] OR "phase 3"[Title/Abstract] OR "phase iii"[Title/Abstract] OR "randomized"[Title/Abstract] OR "guideline"[Title/Abstract] OR "practice guideline"[Title/Abstract] OR "systematic review"[Title/Abstract] OR "meta-analysis"[Title/Abstract] OR "point-of-care"[Title/Abstract] OR "diagnostic accuracy"[Title/Abstract] OR "rapid diagnostic"[Title/Abstract] OR "implementation"[Title/Abstract] OR "hospital epidemiology"[Title/Abstract] OR "infection prevention"[Title/Abstract] OR "antimicrobial stewardship"[Title/Abstract] OR "resistance"[Title/Abstract] OR "molecular epidemiology"[Title/Abstract] OR "vaccine effectiveness"[Title/Abstract] OR "real-world"[Title/Abstract])
```

## 3) Basic/translational science block

```text
("host-pathogen"[Title/Abstract] OR "immune evasion"[Title/Abstract] OR "virulence"[Title/Abstract] OR "pathogenesis"[Title/Abstract] OR "single-cell"[Title/Abstract] OR "organoid"[Title/Abstract] OR "crispr"[Title/Abstract] OR "structural biology"[Title/Abstract] OR "neutralizing"[Title/Abstract] OR "innate immunity"[Title/Abstract] OR "adaptive immunity"[Title/Abstract] OR "microbiome"[Title/Abstract] OR "transcriptomic"[Title/Abstract] OR "proteomic"[Title/Abstract] OR "metabolomic"[Title/Abstract])
```

## 4) Combined term

```text
([Journal block]) AND (([Near-term block]) OR ([Basic/translational block]))
```

Date range is enforced in API parameters (`mindate`, `maxdate`, `datetype=pdat`) rather than by string filter.
