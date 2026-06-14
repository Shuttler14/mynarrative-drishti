# Red Flags: Scraping Indian Fashion E-commerce

## LEGAL RISKS

| Risk | Severity | Mitigation |
|------|----------|------------|
| ToS violation (Myntra/Flipkart/Amazon) | HIGH | Use only for non-commercial research; delete if DMCA |
| Copyright infringement | HIGH | Use images only for training, not redistribution |
| Data Protection Act 2023 | MEDIUM | No PII, only product images |
| CFAA (US) / IT Act 2000 (India) | HIGH | No bypassing security, no credential theft |

## TECHNICAL BLOCKS

### Anti-Bot Systems
| System | Sites | Bypass |
|--------|-------|--------|
| Cloudflare | All 3 | Undetected-chromedriver, residential proxies |
| PerimeterX | Flipkart | Mobile API, random delays |
| DataDome | Amazon | Rotate IPs, solve CAPTCHAs |
| reCAPTCHA v3 | All 3 | Low request rate, human-like behavior |

### Rate Limits
| Site | Limit | Backoff |
|------|-------|---------|
| Myntra | ~60 req/min | Exponential, 2x each failure |
| Flipkart | ~30 req/min | Fixed 5s delay |
| Amazon | ~1 req/sec | 10s delay on 503 |

## DATA QUALITY ISSUES

| Issue | Impact | Fix |
|-------|--------|-----|
| Watermarks | Bad training | OCR detection, skip |
| Mannequin shots | Bad for face preservation | CLIP classify, skip |
| Low resolution | Bad quality | Check dimensions, skip |
| Mixed ethnic/western | Wrong category | CLIP zero-shot classify |
| Duplicate images | Overfitting | Hash deduplication |
| Cropped faces | Face preservation fails | Face detection, skip if no face |

## BRIGHT DATA ALTERNATIVE

Bright Data has pre-built datasets for Indian fashion:
- https://brightdata.com/products/datasets
- Cost: ~$0.001 per image (bulk)
- Quality: Pre-cleaned, model shots, no watermarks
- Legal: Licensed for commercial use

## RECOMMENDATION

For production, **buy licensed data** instead of scraping:
1. Bright Data Indian Fashion Dataset (~$500 for 500K images)
2. Shutterstock/Freepik fashion collections
3. Partner with Indian fashion bloggers (user-generated content)
4. Use AI-generated synthetic data (Stable Diffusion)
