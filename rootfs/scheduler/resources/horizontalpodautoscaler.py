import json
from scheduler.resources import Resource
from scheduler.exceptions import KubeException, KubeHTTPException


class HorizontalPodAutoscaler(Resource):
    api_prefix = 'apis'
    short_name = 'hpa'

    @property
    def api_version(self):
        # API location changes between versions
        # http://kubernetes.io/docs/user-guide/horizontal-pod-autoscaling/#api-object
        if self.version() >= 1.3:
            return 'autoscaling/v1'

        # 1.2 and older
        return 'extensions/v1beta1'

    def get(self, namespace, name=None, **kwargs):
        """
        Fetch a single HorizontalPodAutoscaler or a list
        """
        url = '/namespaces/{}/horizontalpodautoscalers'
        args = [namespace]
        if name is not None:
            args.append(name)
            url += '/{}'
            message = 'get HorizontalPodAutoscaler "{}" in Namespace "{}"'
        else:
            message = 'get HorizontalPodAutoscalers in Namespace "{}"'

        url = self.api(url, *args)
        response = self.session.get(url, params=self.query_params(**kwargs))
        if self.unhealthy(response.status_code):
            args.reverse()  # error msg is in reverse order
            raise KubeHTTPException(response, message, *args)

        return response

    def manifest(self, namespace, name, target, **kwargs):
        min_replicas = kwargs.get('min')
        max_replicas = kwargs.get('max')
        cpu_percent = kwargs.get('cpu_percent')

        if min_replicas < 1:
            raise KubeException('min replicas needs to be 1 or higher')

        if max_replicas < min_replicas:
            raise KubeException('max replicas can not be smaller than min replicas')

        labels = {
            'app': namespace,
            'type': kwargs.get('app_type'),
            'heritage': 'deis',
        }

        manifest = {
            'kind': 'HorizontalPodAutoscaler',
            'apiVersion': self.api_version,
            'metadata': {
                'name': name,
                'namespace': namespace,
                'labels': labels,
            },
            'spec': {
                'minReplicas': min_replicas,
                'maxReplicas': max_replicas,
                'scaleRef': {
                    # only works with Deployments, RS and RC
                    'kind': target['kind'],
                    'name': target['metadata']['name'],
                    # the resource of the above which does the scale action
                    'subresource': 'scale',
                },
                'cpuUtilization': {
                    'targetPercentage': cpu_percent
                }
            }
        }

        return manifest

    def create(self, namespace, name, target, **kwargs):
        manifest = self.manifest(namespace, name, target, **kwargs)

        url = self.api("/namespaces/{}/horizontalpodautoscalers", namespace)
        response = self.session.post(url, json=manifest)
        if self.unhealthy(response.status_code):
            raise KubeHTTPException(
                response,
                'create HorizontalPodAutoscaler "{}" in Namespace "{}"', name, namespace
            )
            self.log(namespace, 'template used: {}'.format(json.dumps(manifest, indent=4)), 'DEBUG')  # noqa

        # optionally wait for HPA if requested
        if kwargs.get('wait', False):
            self.wait(namespace, name)

        return response

    def update(self, namespace, name, target, **kwargs):
        manifest = self.manifest(namespace, name, target, **kwargs)

        url = self.api("/namespaces/{}/horizontalpodautoscalers/{}", namespace, name)
        response = self.session.put(url, json=manifest)
        if self.unhealthy(response.status_code):
            self.log(namespace, 'template used: {}'.format(json.dumps(manifest, indent=4)), 'DEBUG')  # noqa
            raise KubeHTTPException(response, 'update HorizontalPodAutoscaler "{}"', name)

        # optionally wait for HPA if requested
        if kwargs.get('wait', False):
            self.wait(namespace, name)

        return response

    def delete(self, namespace, name):
        url = self.api("/namespaces/{}/horizontalpodautoscalers/{}", namespace, name)
        response = self.session.delete(url)
        if self.unhealthy(response.status_code):
            raise KubeHTTPException(
                response,
                'delete HorizontalPodAutoscaler "{}" in Namespace "{}"', name, namespace
            )

        return response

    def wait(self, namespace, name):
        # fetch HPA details
        hpa = self.hpa.get(namespace, name).json()

        # FIXME all of the below can be replaced with hpa['status'][desiredReplicas']
        # when https://github.com/kubernetes/kubernetes/issues/29739 is fixed
        # until then we have to query things ourselves

        # only wait 30 seconds / attempts - this is not optimal
        # ideally it would use the resources wait commands but they vary
        for _ in range(30):
            # fetch resource attached to it
            resource_kind = hpa['spec']['scaleRef']['kind'].lower()
            resource_name = hpa['spec']['scaleRef']['name']

            resource = getattr(self, resource_kind)
            resource = getattr(resource, 'get')(namespace, resource_name).json()

            # compare resource current replica count to HPA
            # (Deployment vs RC vs RS is all different)
            if resource_kind in ['replicationcontroller', 'replicaset']:
                replicas = resource['status']['replicas']
            elif resource_kind == 'deployment':
                replicas = resource['status']['availableReplicas']

            if replicas <= hpa['spec']['maxReplicas'] or replicas >= hpa['spec']['minReplicas']:
                break
