# LeakTrace V2 Launch Features

This package is a real drop-in update made against the uploaded LeakTrace Flask app.

## Added

- Contractor-style case/customer file fields
- Case status and priority tracking
- Assigned technician field
- Insurance company, claim number, and adjuster fields
- Contractor company/license/contact fields
- Editable case file on the results page
- Upgraded case dashboard with metrics, filters, and search
- Professional PDF report generation
- Separate insurance/carrier PDF report generation
- Latest field verification display
- Database-safe migrations using `ALTER TABLE ADD COLUMN`

## Important

This zip intentionally excludes:

- `.env`
- `.git`
- `venv`
- `leaktrace.db`
- `static/uploads`
- `generated_reports`

Your existing Render database and uploaded files should stay in place. On first app startup, `init_db()` will add the new columns if they do not already exist.

## After copying over

Run locally if possible:

```bash
python app.py
```

Then commit and push:

```bash
git add .
git commit -m "Add LeakTrace launch case management and reports"
git push origin main
```
