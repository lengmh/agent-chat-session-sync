# Windows worker uses the current user's Task Scheduler

The Windows worker runs as a limited, interactive current-user Scheduled Task instead of an SCM Service. Native Agent Sessions, cc-connect configuration, credentials, and desktop hooks belong to that user profile; moving the worker into Session 0 or a service account would change those ownership and credential boundaries.
