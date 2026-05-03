"""
A very simple key-value database using Python's pickle module.
It supports basic operations like create, read, update, delete.
It also supports nested key paths for updates.
"""

import os
import pickle
from copy import deepcopy
from os import path

DEFAULT_DATA = {
    'browser': 'edge',
    'argdict': {
        'ca': '',
        'classname': '',
        'path': '',
        'video_resolution': '720p',
        'sl': 'en'
    }
}


def _merge_defaults(data, defaults):
    for key, value in defaults.items():
        if isinstance(value, dict):
            existing = data.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            data[key] = _merge_defaults(existing, value)
        else:
            data.setdefault(key, value)
    return data


class SimpleDB:
    def __init__(self, filename='data.bin'):
        self.filename = path.abspath(path.join(path.dirname(__file__), filename)) # use this, so that, in bundled package the file is in the same directory as the script also
        self._data = self._load()

    def _load(self):
        """Load data from the file, or initialize an empty dict if file doesn't exist."""
        if not os.path.exists(self.filename):
            self._save(deepcopy(DEFAULT_DATA))

        try:
            with open(self.filename, 'rb') as f:
                data = pickle.load(f)
        except (EOFError, OSError, pickle.PickleError):
            data = {}

        if not isinstance(data, dict):
            data = {}

        data = _merge_defaults(data, deepcopy(DEFAULT_DATA))
        self._save(data)
        return data

    def _save(self, data):
        """Save current data to file."""
        with open(self.filename, 'wb') as f:
            pickle.dump(data, f)

    def create(self, key, value):
        """Create a new key-value pair. Raises error if key exists."""
        if key in self._data:
            raise KeyError(f"Key '{key}' already exists.")
        self._data[key] = value
        self._save(self._data)

    def read(self, key):
        """Read the value for a given key. Returns None if not found."""
        return self._data.get(key, None)

    def update(self, key_path, value):
        """Update value at top-level key or nested key path.
        example key_path: 'argdict.ca' or ['argdict', 'ca'].
        Raises KeyError if the key path is invalid.
        """
        if isinstance(key_path, str):
            key_path = key_path.split('.')  # support dot notation

        data_ref = self._data
        for key in key_path[:-1]:
            if key not in data_ref or not isinstance(data_ref[key], dict):
                raise KeyError(f"Path '{'.'.join(key_path)}' is invalid.")
            data_ref = data_ref[key]

        final_key = key_path[-1]
        if final_key not in data_ref:
            raise KeyError(f"Key '{final_key}' not found in path '{'.'.join(key_path)}'.")
        data_ref[final_key] = value
        self._save(self._data)

    def upsert(self, key_path, value):
        """Create or update a top-level or nested key path."""
        if isinstance(key_path, str):
            key_path = key_path.split('.')

        data_ref = self._data
        for key in key_path[:-1]:
            data_ref = data_ref.setdefault(key, {})
            if not isinstance(data_ref, dict):
                raise KeyError(f"Path '{'.'.join(key_path)}' is invalid.")

        data_ref[key_path[-1]] = value
        self._save(self._data)


    def delete(self, key):
        """Delete a key-value pair."""
        if key in self._data:
            del self._data[key]
            self._save(self._data)
        else:
            raise KeyError(f"Key '{key}' not found.")

    def get_full_db(self):
        """Return the full dictionary."""
        return dict(self._data)

if __name__ == '__main__':
    db = SimpleDB()

    # db.create('username', 'alice')
    # print(db.read('username'))  # alice

    # db.update('username', 'bob')
    # print(db.read('username'))  # bob

    # db.delete('username')
    # print(db.read('username'))  # None

    print(db.get_full_db())        # {}
