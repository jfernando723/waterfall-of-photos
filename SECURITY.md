# Security

**Assume this software is not secure.** It is a personal project; the code was written
by an AI and has never been reviewed by anyone able to judge it. It is unlikely to be
updated, so problems found later, in it or in the software it is built on, will most
likely stay unfixed. The author does not recommend that anyone use it.

What follows describes what it was meant to do. None of it is a promise.

## What Waterfall is built to withstand, and what it is not

Waterfall was made for one household: a NAS at home, reached over the home network or
a private VPN. Read this before putting it anywhere else.

| | |
|---|---|
| **Do not expose it to the internet.** | It speaks plain http. The admin password, the sign-in cookie and every picture cross the network unencrypted. Use your own network, or a private VPN. If you must publish it, put it behind a reverse proxy that adds https and its own sign-in, and set `WATERFALL_TRUSTED_PROXY`. |
| **The Visible wall is open by design.** | With no viewing password set, anyone who can reach port 7275 sees everything under `Visible`, including each item's date, camera and exact GPS position. Set a viewing password if that is not what you want. |
| **The first visitor chooses the admin password.** | Until one is set, whoever opens the address first sets it. Open it yourself straight after starting the container, or set `WATERFALL_ADMIN_PASSWORD` for the first start. |
| **One password, no accounts.** | Six wrong guesses from one address lock that address out for five minutes. Behind Docker on some NAS systems every visitor appears to come from the same address, so six wrong guesses by anyone lock sign-in for everyone for those five minutes. Someone on your network can use that to keep you from signing in, though not to get in. Choose a long password: the minimum is six characters and that is too short for anything but a home network. |
| **The admin is signed out after half an hour untouched.** | So that a phone put down unlocked does not go on showing the Hidden wall. The page leaves by itself, and the server stops honouring the sign-in at the same moment whether or not the page is still open. An upload already under way finishes: the page holds a ticket that lets it add files and nothing else, for a day at most. |
| **Signing out forgets the cookie, not the pictures.** | A browser keeps what it has already downloaded. On a shared phone or computer, hidden pictures you looked at stay in that browser's cache until it is cleared. |
| **The import door is for the machine itself.** | The compose file publishes port 7276 on `127.0.0.1` only. If you open it to your network, the admin password crosses that network unencrypted with every file copied. |
| **The admin can upload from the page.** | Files sent that way arrive on port 7275 in pieces of 4 MB, and only the admin's are kept. But the server has to read a request before it can tell who sent it, so anyone who can reach the port can make it read 4 MB at a time, on up to 48 connections at once: about 200 MB held briefly in memory, at worst. Before uploads existed that ceiling was 64 KB a request. |
| **It converts whatever you copy in.** | Photos and videos are opened by Pillow, ffmpeg and ExifTool. A file made to attack one of those could, at worst, act as Waterfall inside its container: read, damage or send away the library, and reach whatever the container can reach on your network. It could not touch any other file on the machine, because the container is given no folder, no capabilities and a read-only system. Only import files you would open on your own computer, and rebuild or update the image now and then so Debian's fixes arrive. |
| **Hidden means "needs the admin password", not "encrypted".** | Hidden items sit in the same Docker volume as everything else. Anyone with administrator access to the machine can read them. |

What has been done about the rest is in the tests (`tests/test_waterfall.py`): nothing
under Hidden is reachable without the admin password by any address; requests that
change anything must come from Waterfall's own page; the import door cannot be climbed
out of or lied to about sizes, serves a fixed number of connections at once and makes
the rest wait or turns them away; an upload from the page needs the admin, stays inside
the two walls and never overwrites anything; and a file cannot send the converter to the
network.

## Reporting a problem

There is no support and no promise that a report will be read or acted on. If you
have found something that puts other people's photos at risk, please do not post the
details in a public issue: use **Security → Report a vulnerability** on this
repository if it is offered. If it matters to you that the problem is fixed, fix it in
your own copy; the licence lets you.
