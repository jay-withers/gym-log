environment = "dev"

custom_domain_name = "health.jaywithers.uk"

insight_email_from = "Gym log <insights@jaywithers.uk>"

# Entra sign-in in front of the app (main.container-apps-auth.tf). From
# azure-landingzone governance's `app_auth_client_ids` output, key `gymlog`.
auth_client_id = "64d46ac9-cb67-43d2-93f1-7a69427b7766" # gitleaks:allow (a client ID, not a secret)
