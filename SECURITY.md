# Security

HealthHub stores people's health data and is usually reachable from the internet, so security problems are taken
seriously.

## Reporting a problem

Please **don't open a public issue**. Report it privately: on the repository page, open the **Security** tab and
choose **Report a vulnerability**. Include what you found, how to reproduce it and the commit you tested. You'll
get a reply as soon as the maintainer can, and credit if you'd like it.

## What counts

- Reading or changing another person's data, or seeing more of other members than the app shows.
- Creating an account without a valid invite, or taking over someone's sign-in.
- Getting around the sign-in throttling, or guessing download links or handoff codes.
- Making the server crash, hang, or fill its disk with a small number of requests.
- Leaking secrets or personal data into responses or logs.

## Running it safely

- Put HealthHub behind HTTPS for anything beyond your home network.
- Use a long, random invite code (`deploy.sh` generates one) and share it only with your group.
- Keep `/etc/healthhub.env` readable by root only, and keep the server updated.
- The admin (first account) can see everyone's history: choose who runs your server.

## Supported versions

Only the latest `main` gets fixes.
