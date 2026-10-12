def personalize_email(template, data):
    for key, value in data.items():
        template = template.replace(f"{{{key}}}", value)
    return template
