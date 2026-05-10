# Goodreads to StoryGraph Sync Setup Guide

Caveat:  This is a complete hack that I made using Claude.ai to write the Python code for me.  I hope it works for you, but I can't guarantee success. 

I use it on Windows, but you should be able to run it on a Mac or Linux computer, as long as you can get Python & pip running.

Note: this script syncs books you've **rated** on Goodreads (the entries in your updates feed of the form "gave N stars to..."). Books you finish without rating won't be picked up.

## Prerequisites

- Python 3.8 or higher
- Google Chrome browser installed
- Windows 11 operating system
- Basic familiarity with running Python scripts

## Installation Steps

1. Create a new directory for the project:
```bash
mkdir goodreads-sync
cd goodreads-sync
```

2. Install required Python packages:
```bash
pip install -r requirements.txt
```

## File Structure

Copy the files into the directory (clone the repo):
```
goodreads-sync/
│
├── book_sync.py    # The main Python script
├── config.json      # Configuration file with your credentials
└── sync_log.txt    # Will be created automatically when the script runs
```

## Configuration

1. Edit `config.json` with the your information:
```json
{
    "goodreads_user_id": "YOUR_GOODREADS_USER_ID",
    "storygraph_email": "YOUR_STORYGRAPH_EMAIL",
    "storygraph_password": "YOUR_STORYGRAPH_PASSWORD"
}
```

### Finding Your Goodreads User ID
1. Go to your Goodreads profile
2. Look at the URL - it will be something like: `https://www.goodreads.com/user/show/12345678-username`
3. The number (e.g., `12345678`) is your user ID

## Running the Script

1. Make sure you're in the project directory:
```bash
cd goodreads-sync
```

2. Run the script:
```bash
python book_sync.py
```

## Web UI (optional)

Instead of running `book_sync.py` directly, you can launch a small local web frontend that wraps the sync and adds a second feature for visualizing the last 12 months of reading.

```bash
python app.py
```

This opens `http://127.0.0.1:5000` in your browser with two buttons:

- **Sync to StoryGraph** — runs `book_sync.py` as a subprocess. Chrome opens exactly as today and the log streams to the page.
- **Generate Year in Books** — fetches your Goodreads read shelf and renders a one-page visualization in three formats:
  - `output/year_in_books.pdf` — Letter portrait, vector
  - `output/year_in_books_web.png` — 1200×1800
  - `output/year_in_books_social.png` — 1080×1920, sized for Instagram Stories

The Year in Books feature uses the public Goodreads RSS for your read shelf and the Google Books API for genre data. No additional credentials are needed beyond the `goodreads_user_id` already in `config.json`.

The server binds to `127.0.0.1` only — it isn't reachable from other machines on your network. Generated files in `output/` are gitignored.

## What to Expect

- The script will create a log file (`sync_log.txt`) that tracks all operations
- Chrome will open automatically and handle the sync process
- The script will:
  1. Fetch your recently rated books from Goodreads
  2. Log into your StoryGraph account
  3. Add each book to your StoryGraph reading journal with the correct completion date

## Troubleshooting

If you encounter errors:
1. Check `sync_log.txt` for detailed error messages
2. Verify your Goodreads ID and StoryGraph credentials in `config.json`
3. Ensure all required Python packages are installed
4. Make sure Chrome is up to date
5. Look for screenshot files (e.g., `login_error.png` or `book_error_*.png`) that may have been created during errors

## Best Practices

1. Keep your `config.json` secure and never share it
2. Run the script periodically (e.g., weekly) to keep your StoryGraph journal up to date
3. Monitor the log file for any issues
4. Update Python packages periodically to ensure compatibility

## Safety Notes

- The script stores your StoryGraph password in plain text in `config.json`
- Keep the config file secure and don't share it

## Support

If you encounter issues:
1. Check the log file for error messages
2. Verify your credentials
3. Ensure all prerequisites are installed
4. Try running the script again after a few minutes
