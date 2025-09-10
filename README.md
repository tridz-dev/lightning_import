# Lightning Import

Lightning Import is a **Frappe app** designed to simplify **bulk data import and updates** into your ERPNext/Frappe system.  
It helps users upload data (e.g., Items, Customers, Transactions) efficiently, with built-in validation and optimized SQL operations.

---

## Features

- Import records from CSV/Excel into any Frappe Doctype  
- Perform **bulk update** operations    
- Track imports via the `Lightning Upload` doctype  
- Error handling and validation before commit  

---

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app lightning_import
```

### Hook Configuration

To enable Lightning Import, you need to configure hooks in your app.
Open your app’s hooks.py and add the following:

```
# hooks.py

# Attach custom JS (optional if you have client-side features)
doctype_js = {
    "Lightning Upload": "public/js/lightning_upload.js"
}

# Trigger import logic when a Lightning Upload is submitted
doc_events = {
    "Lightning Upload": {
        "on_submit": "lightning_import.lightning_import.doctype.lightning_upload.lightning_upload.run_import"
    }
}
```

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/lightning_import
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
