# Senators-Stocks

Posílá do aplikace **ntfy** česky, jaké akcie nakupují a prodávají američtí senátoři
a kongresmani. Data pocházejí z povinných hlášení podle zákona **STOCK Act**, která
zpracovává [Financial Modeling Prep](https://site.financialmodelingprep.com/).

Kontrola probíhá každé 3 hodiny. Upozornění přijde jen tehdy, když jsou zveřejněny nové obchody.

## Nastavení

1. **ntfy:** Nainstaluj aplikaci ntfy (Android / iOS) → „+“ → přihlas se k odběru tématu
   `senat-akcie-yckQ8xLDyxkG006JwR5wJI90` na serveru `ntfy.sh`.
   Téma funguje jako heslo. Kdo ho zná, může zprávy číst, proto ho nesdílej.
2. **API klíč:** Zaregistruj se zdarma na https://site.financialmodelingprep.com/register.
3. **GitHub:** Settings → Secrets and variables → Actions → *New repository secret*
   `FMP_API_KEY` = tvůj klíč.
4. Actions → „Obchody Kongresu → ntfy“ → *Run workflow* (první test). Potom už workflow
   běží samo každé 3 hodiny.

## Spuštění na vlastním počítači (alternativa)

```bash
export FMP_API_KEY=tvuj_klic
python senators_stocks.py --dry-run   # jen náhled
python senators_stocks.py --loop      # běží stále, kontrola každé 3 hodiny
```

Volitelné proměnné: `ONLY_PURCHASES=1` (jen nákupy), `NOTIFY_EMPTY=1` (zpráva i bez
nových obchodů), `NTFY_TOPIC`, `NTFY_SERVER`.

Pozn.: Zákon dává politikům až 45 dní na nahlášení obchodu, takže data mají zpoždění.
