# TapelessIngest - Development Guide

## Prerequisites

### System Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.8+ | Cantemo Portal Python version |
| Django | 2.x/3.x | Via Cantemo Portal |
| Cantemo Portal | 5.x+ | Plugin host platform |
| Vidispine | 5.x+ | Media asset management |
| FFmpeg | 4.x+ | Thumbnail generation |
| Elasticsearch | 7.x | File indexing |

### Development Environment

1. **Cantemo Portal Development Instance**
   - Local or remote Portal installation
   - Admin access for plugin development
   - Vidispine API access

2. **Python Environment**
   - Use Portal's Python environment
   - No separate virtualenv required

3. **IDE Setup**
   - Python/Django support
   - Recommended: PyCharm, VS Code with Python extension

## Installation

### Plugin Location

```bash
# Navigate to Portal plugins directory
cd /opt/cantemo/portal/portal/plugins/

# Clone or copy plugin
cp -r /path/to/TapelessIngest ./TapelessIngest
```

### Database Migrations

```bash
# From Portal directory
cd /opt/cantemo/portal

# Run migrations
python manage.py migrate TapelessIngest
```

### Static Files

```bash
# Collect static files
python manage.py collectstatic --noinput
```

### Restart Portal

```bash
# Restart Portal services
sudo systemctl restart portal
# or
sudo service portal restart
```

## Project Structure

```
TapelessIngest/
├── plugin.py           # Plugin registration
├── urls.py             # URL routing
├── views.py            # API views
├── models/             # Django models
├── providers/          # Camera format handlers
├── serializers.py      # REST serializers
├── templates/          # HTML templates
├── static/             # JS/CSS assets
└── migrations/         # DB migrations
```

## Development Workflow

### 1. Making Changes

```bash
# Edit files in plugin directory
vim /opt/cantemo/portal/portal/plugins/TapelessIngest/views.py
```

### 2. Testing Changes

For Python changes, restart Portal:
```bash
sudo systemctl restart portal
```

For template/static changes, collect static:
```bash
python manage.py collectstatic --noinput
```

### 3. Checking Logs

```bash
# Portal logs
tail -f /var/log/cantemo/portal/portal.log

# Django debug
tail -f /var/log/cantemo/portal/django.log
```

## Code Style

### Python

- Follow PEP 8
- Use logging instead of print statements
- Type hints encouraged (recent improvements)

```python
import logging

log = logging.getLogger(__name__)

def process_clip(clip: Clip) -> bool:
    """Process a single clip for ingestion."""
    log.debug(f"Processing clip: {clip.umid}")
    # ... implementation
```

### Django Patterns

- Use Django ORM querysets
- Leverage model properties for computed values
- Use Django REST Framework for API endpoints

```python
# Good: Using properties
@property
def storage(self):
    if not hasattr(self, "_storage"):
        self._storage = StorageHelper().getStorage(self.storage_id)
    return self._storage

# Good: Using DRF serializers
class ClipSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clip
        fields = ("umid", "path", "status")
```

## Adding a New Provider

1. **Create provider file**

```python
# providers/myformat.py
from portal.plugins.TapelessIngest.providers.providers import Provider

class MyFormatProvider(Provider):
    def __init__(self, folder=None):
        super().__init__(folder)
        self.name = "My Format"
        self.machine_name = "myformat"
    
    def getExtensions(self):
        return [".myf", ".mfx"]
    
    def getSubPaths(self):
        return ["CLIP", "VIDEO"]
    
    def getFilters(self, escaped_path):
        return [
            {"regexp": {"path": f"{escaped_path}/.*\\.myf$"}}
        ]
    
    def getMetadatasFromFile(self, file, metadatas, context):
        # Extract metadata from file
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = self.generate_umid(file)
        return metadatas, context
```

2. **Register provider**

Add to `models/clip.py`:
```python
PROVIDERS_LIST = [
    "red",
    "panasonicP2",
    # ... existing providers
    "myformat",  # Add new provider
]
```

3. **Test provider**

```python
# In Django shell
from portal.plugins.TapelessIngest.models.clip import Clip
provider = Clip.get_provider_by_name("myformat")
print(provider.getExtensions())
```

## API Development

### Adding New Endpoint

1. **Create view**

```python
# views.py
class MyNewView(APIView):
    renderer_classes = (JSONRenderer,)
    
    def get(self, request):
        # Implementation
        return Response({"status": "ok"})
```

2. **Register URL**

```python
# urls.py
urlpatterns = [
    # ... existing urls
    url(r"^api/mynewview$", views.MyNewView.as_view()),
]
```

### Serializer Patterns

```python
# serializers.py
class MySerializer(serializers.ModelSerializer):
    computed_field = serializers.SerializerMethodField()
    
    class Meta:
        model = MyModel
        fields = ("id", "name", "computed_field")
    
    def get_computed_field(self, obj):
        return obj.calculate_something()
```

## Testing

### Manual Testing

```python
# Django shell
python manage.py shell

>>> from portal.plugins.TapelessIngest.models.folder import Folder
>>> folder = Folder.objects.first()
>>> folder.scan()
```

### API Testing

```bash
# Using curl
curl -u admin:password \
  -X PUT \
  -H "Content-Type: application/json" \
  -d '{"paths": [{"path": "/test", "storage": "VX-1"}]}' \
  http://portal/tapelessingest/api/browser/clips
```

## Debugging

### Enable Debug Logging

In Portal settings or `local_settings.py`:

```python
LOGGING['loggers']['portal.plugins.TapelessIngest'] = {
    'handlers': ['file'],
    'level': 'DEBUG',
}
```

### Common Issues

| Issue | Solution |
|-------|----------|
| Provider not found | Check PROVIDERS_LIST in clip.py |
| API 500 error | Check portal.log for stack trace |
| Static not loading | Run collectstatic |
| Migration errors | Check migration order/dependencies |

## Performance Considerations

### Recent Improvements

- **Redis Caching**: Storage/collection lookups cached (~70% reduction in API calls)
- **Provider Caching**: Provider instances reused
- **Batch Operations**: Process clips in batches

### Best Practices

1. Use `select_related()` / `prefetch_related()` for queries
2. Leverage Elasticsearch for file searches
3. Use async jobs for long operations
4. Cache expensive Vidispine API calls

## Deployment

### Production Checklist

- [ ] Run migrations
- [ ] Collect static files
- [ ] Verify settings in admin
- [ ] Test with sample media
- [ ] Check logs for errors
- [ ] Restart Portal services

### Configuration

1. Access Admin > TapelessIngest
2. Set default storage
3. Configure metadata mappings
4. Set collection rules

## Resources

- [PROVIDERS.md](../PROVIDERS.md) - Provider development guide
- [API.md](../API.md) - API documentation
- [Cantemo Developer Docs](https://portal.cantemo.com/docs/)
- [Django Documentation](https://docs.djangoproject.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
