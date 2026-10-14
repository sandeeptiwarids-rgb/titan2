"""titan2 dashboard (spec §12): a read-only view of the bot's pub/*.json files plus three operator commands.

Served by gunicorn as `titan2.dash.app:app` (user titandash, 127.0.0.1:8050, reached over an SSH tunnel).
Nothing is imported here, so importing the package never builds the app or needs the dashboard's secrets.
"""
