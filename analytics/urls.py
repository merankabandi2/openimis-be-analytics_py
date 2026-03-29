from django.urls import path
from . import views

urlpatterns = [
    path('download/<uuid:export_id>/', views.download_export, name='analytics_download_export'),
    path('entity-schema/<str:entity_type>/', views.get_entity_schema, name='analytics_entity_schema'),
]