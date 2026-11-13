## MARIJUS:

Mano nuomone turi būti tokios funkcijos:

1. Paprastas straipsnio patikrinimas (jei darome lokaliai, mums vertetu tureti offline sprendima ziniu intencijos patikrinimui.) Taip galima būtų ištirti apskritai semantinį toną paslėptą straipsnyje, panašiai kaip ground news.
2. Straipsni šaltinio patikrinimas (scorinamas žurnalistas / žmogus, tikrinama apskritai su ground truth. T.y. AI pagooglina ar tai tiesa).
3. Straipsnio šaltinio istorijos atsekimas (naudojant oxylabs ar panašiai atsekti, iš kokių šaltinių straipsniai ateina.) Pvz. Rusų medija -> telegram grupė x -> telegram grupė y -> OP Tv janutienė. Parodyti tai naudotojui, kaupti statistiką.
4. Statistikos apžvalga (žmonių / žurnalistų flagginimas, tikrinimas, ar žmogus nebotas)

Pagalbiniai dalykai:

1. Turi veikti kaip google extention, bet turi egzistuoti ir native app. Patogiausia, kad nereiktų žinių scrollinti per mūsų appsą, bet jis visą info gautų.
2. DI paleistas lokaliai - jis turi būti išmokytas ne tik paprastų propagandos analizės metodų, jis turi padėti naudotojui ir su bendriniais klausimais pavojaus metu (kadangi siekiama, kad būtų ir lokaliai runnintų). Į šią info įeina pasiruošimas karui, pirmoji pagalba.
3. Semantinis šaltinių vertinimas DI. Jei sutampa net kelių skirtingų kalbų bus flagginti. Qdrant incoming!
4. lokaliam useriui automatinis modelio parinkimas, atsisiuntimas ir konfiguravimas su llama cpp + llama swap
