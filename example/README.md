# Example Store

A minimal swag store demonstrating the Merchant API.

## Setup

1. **Start the Merchant API** (from repo root):

   ```bash
   npm run dev
   # API at http://localhost:8000
   ```

2. **Initialize API keys** (first run only):

   ```bash
   npm run init
   ```

3. **Seed products** (optional):

   ```bash
   npm run seed sk_your_admin_key
   ```

4. **Create your API config**:

   ```bash
   cp src/api.example.js src/api.js
   ```

   Edit `src/api.js` — set `PUBLIC_KEY` to your `pk_...` key from init output.

5. **Start the store**:

   ```bash
   cd example
   npm install && npm run dev
   ```

6. Open http://localhost:3000

## How It Works

- **Products** — Fetched from `/v1/products` using the public key
- **Cart** — Stored in localStorage, synced to Merchant cart on checkout
- **Checkout** — Creates a Merchant cart, then redirects to Stripe Checkout
- **Success** — Shows confirmation after payment

## Files

```
index.html      → Product listing
cart.html       → Shopping cart
success.html    → Post-checkout confirmation
src/
  api.example.js → API config template (copy to api.js)
  api.js          → Your local config (gitignored)
  cart.js         → Cart state management
  main.js         → Product listing logic
  cart-page.js    → Cart page logic
```

## Customization

- Update `src/api.js` with your API URL (`http://localhost:8000`) and `pk_...` key
- Replace product images in the UI (or use image URLs from your products)
- Modify styles directly in HTML (Tailwind via CDN)
