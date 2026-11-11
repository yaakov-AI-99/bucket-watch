# Bucket Watch

Tracks collectible movie-theater popcorn buckets: which ones are worth flipping on eBay, when they go on sale, and the closest theater to work (Hewlett, NY) for opening night.

- `index.html` – the page (served by GitHub Pages, embedded in Google Sites)
- `data.json` – live resale data the page reads (eBay asks vs. confirmed "X sold", watchers, Google Trends)
- `tracker.py` – pulls the data from SerpApi and estimates profit per bucket after eBay fees
- `.github/workflows/update-data.yml` – runs the tracker every morning and commits `data.json`

## One-time setup

1. **Add your SerpApi key** – repo **Settings → Secrets and variables → Actions → New repository secret**. Name: `SERPAPI_KEY`, value: the key from https://serpapi.com/manage-api-key.
2. **Turn on GitHub Pages** – **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`, Save. After a minute the page is live at `https://<your-username>.github.io/<repo-name>/`.
3. **Test the daily update** – **Actions → Update resale data → Run workflow**. It takes about 2 minutes and commits a fresh `data.json`.

## Put it on your Google Site

1. Open your site in Google Sites and go to the page you want.
2. On the right, under **Insert**, click **Embed** → **By URL**.
3. Paste `https://<your-username>.github.io/<repo-name>/` and choose **Whole page**. Click **Insert**.
4. Drag the embed's corner to make it full width and tall (about 2000 px). Click **Publish**.

The embed updates on its own every day because it shows the live GitHub Pages page.

## Run it yourself

```bash
SERPAPI_KEY=your_key python3 tracker.py data.json
```

Each run uses about 10 SerpApi searches (about 300 a month on a daily schedule).

## How profit is estimated

- If a bucket has 3 or more confirmed sales on eBay ("X sold" on listings), the sell price is the sales-weighted average of those.
- Otherwise it is 80% of the median asking price, marked **low confidence**.
- Profit = sell price − eBay's 13.25% fee (on price + shipping) − $0.40 order fee − theater price. Buyer pays shipping.
- Theater prices for buckets that haven't been announced are estimates in `ITEMS` at the top of `tracker.py`.
