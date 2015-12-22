from django import template
register = template.Library()


@register.filter(name='show_status')
def show_status(value, css = False):
    if value == 0:
        return "Not imported"
    if value == 1:
        return "Copied"
    if value == 2:
        return "Placeholder created"
    if value == 3:
        return "Imported"

@register.filter(name='show_status_class')
def show_status_class(value):
    if value == 0:
        return "NOT_IMPORTED"
    if value == 1:
        return "GROWING"
    if value == 2:
        return "GROWING"
    if value == 3:
        return "IMPORTED"

@register.filter(name='frame_to_time')
def frame_to_time(value):
    import datetime
    seconds = value/25
    return str(datetime.timedelta(seconds=seconds))